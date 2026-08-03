use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Component, Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    process::{ChildStderr, ChildStdout, Command as AsyncCommand},
};

use crate::Root;

const MAX_BYTES: u64 = 4 * 1024 * 1024;
const BLOCKED_NAMES: &[&str] = &[".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"];
const MAX_COMMAND_LEN: usize = 2000;
const DEFAULT_COMMAND_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_COMMAND_TIMEOUT_SECS: u64 = 600;
const MAX_OUTPUT_BYTES: usize = 1024 * 1024;

const ALLOWED_PROGRAMS: &[&str] = &[
    "git", "npm", "pnpm", "yarn", "node", "cargo", "rustc", "go", "dotnet", "java", "javac", "python", "py", "rg", "ls",
    "dir", "find", "findstr", "pwd", "tree", "where", "cmake", "make", "ninja", "mvn", "gradle", "gcc", "g++", "clang",
    "clang++", "tsc", "eslint", "prettier",
];
const DENIED_PROGRAMS: &[&str] = &[
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "wsl", "rm", "del", "erase", "rmdir", "rd", "remove-item", "curl",
    "wget", "iwr", "invoke-webrequest", "ping", "taskkill", "start-process", "reset", "npx", "docker", "docker-compose",
];
const DENIED_GIT_SUBCOMMANDS: &[&str] = &[
    "reset", "clean", "push", "clone", "restore", "rm", "mv", "gc", "prune", "filter-branch", "reflog", "replace",
];
const AUTO_GIT_SUBCOMMANDS: &[&str] = &[
    "status", "log", "diff", "show", "branch", "remote", "ls-files", "grep", "rev-parse", "tag", "stash", "describe",
    "shortlog", "blame", "config",
];
const APPROVAL_GIT_SUBCOMMANDS: &[&str] = &[
    "add", "commit", "merge", "rebase", "cherry-pick", "revert", "fetch", "pull", "checkout", "switch", "init", "apply",
    "archive", "notes", "submodule", "worktree",
];
const AUTO_BUILD_TEST_SCRIPTS: &[&str] = &["test", "build", "lint", "typecheck", "check"];
const LONG_RUNNING_SCRIPTS: &[&str] = &["dev", "start", "serve", "watch", "preview", "storybook"];
const NPM_NETWORK_SUBCOMMANDS: &[&str] = &[
    "install", "i", "add", "ci", "update", "outdated", "audit", "uninstall", "remove", "rebuild", "dedupe",
];
const NPM_PUBLISH_SUBCOMMANDS: &[&str] = &["publish", "login", "logout", "whoami"];
const CARGO_NETWORK_SUBCOMMANDS: &[&str] = &["update", "search", "add", "remove"];
const CARGO_DENIED_SUBCOMMANDS: &[&str] = &["install", "publish"];
const AUTO_LIST_PROGRAMS: &[&str] = &["ls", "dir", "rg", "find", "findstr", "pwd", "tree", "where"];
const VERSION_PROGRAMS: &[&str] = &[
    "git", "npm", "pnpm", "yarn", "node", "cargo", "rustc", "python", "py", "go", "dotnet", "java", "tsc", "eslint",
    "prettier", "mvn", "gradle", "cmake", "ninja",
];

fn digest(value: &[u8]) -> String { format!("{:x}", Sha256::digest(value)) }

fn validate_approval_token(payload: &Value) -> Result<()> {
    let token = payload["approvalToken"].as_str().context("缺少审批令牌")?;
    let token_hash = payload["approvalTokenHash"].as_str().context("缺少审批令牌哈希")?;
    if token.len() < 16 || token_hash.len() != 64 || !token_hash.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        bail!("审批令牌无效，拒绝执行");
    }
    if digest(token.as_bytes()) != token_hash {
        bail!("审批令牌校验失败，拒绝执行");
    }
    Ok(())
}

fn root<'a>(roots: &'a [Root], id: &str) -> Result<&'a Root> {
    roots.iter().find(|root| root.id == id).context("未授权的项目根目录")
}

fn safe_path(root: &Root, relative: &str) -> Result<PathBuf> {
    let candidate = Path::new(relative);
    if candidate.is_absolute() || candidate.components().any(|component| matches!(component, Component::ParentDir | Component::RootDir | Component::Prefix(_))) {
        bail!("路径越界：仅允许访问授权项目目录内的文件")
    }
    let resolved = fs::canonicalize(root.path.join(candidate)).or_else(|_| Ok::<PathBuf, std::io::Error>(root.path.join(candidate)))?;
    let base = fs::canonicalize(&root.path)?;
    if !resolved.starts_with(&base) { bail!("路径越界（含符号链接逃逸）：仅允许访问授权项目目录内的文件") }
    Ok(resolved)
}

fn verify_file(path: &Path) -> Result<()> {
    let metadata = match fs::metadata(path) {
        Ok(value) => value,
        Err(_) => bail!("文件不存在：{}", path.display()),
    };
    if !metadata.is_file() { bail!("不是普通文件（目录或特殊文件）：{}", path.display()) }
    if metadata.len() > MAX_BYTES { bail!("文件超过 {} MB 大小限制，无法读取：{}", MAX_BYTES / 1024 / 1024, path.display()) }
    let name = path.file_name().and_then(|value| value.to_str()).unwrap_or("");
    if BLOCKED_NAMES.iter().any(|blocked| name.eq_ignore_ascii_case(blocked)) || name.ends_with(".pem") || name.ends_with(".key") || name.ends_with(".p12") {
        bail!("出于安全考虑，禁止访问 .env、私钥等敏感文件：{}", path.display())
    }
    Ok(())
}

pub async fn execute(roots: &[Root], payload: &Value) -> Result<Value> {
    let kind = payload["kind"].as_str().context("缺少任务类型")?;
    let root = root(roots, payload["rootId"].as_str().context("缺少 rootId")?)?;
    match kind {
        "list_files" => list(root, payload),
        "read_file" => read(root, payload),
        "stage_patch" => stage(root, payload),
        "apply_patch" => apply(root, payload),
        "run_command" => run_command(root, payload).await,
        _ => bail!("不支持的任务"),
    }
}

fn list(root: &Root, payload: &Value) -> Result<Value> {
    let start = safe_path(root, payload["relativePath"].as_str().unwrap_or(""))?;
    let mut entries = Vec::new();
    for entry in fs::read_dir(start)? {
        if entries.len() >= 500 { break; }
        let entry = entry?;
        let metadata = entry.metadata()?;
        entries.push(json!({"name": entry.file_name().to_string_lossy(), "directory": metadata.is_dir(), "size": if metadata.is_file() { metadata.len() } else { 0 }}));
    }
    Ok(json!({"entries": entries}))
}

fn read(root: &Root, payload: &Value) -> Result<Value> {
    let relative = payload["relativePath"].as_str().context("缺少相对路径")?;
    let path = safe_path(root, relative)?;
    verify_file(&path)?;
    let bytes = fs::read(&path)?;
    let content = String::from_utf8(bytes.clone()).map_err(|_| anyhow::anyhow!("文件不是 UTF-8 编码的文本，无法读取：{}", path.display()))?;
    Ok(json!({"relativePath": relative, "content": content, "sha256": digest(&bytes)}))
}

fn stage(root: &Root, payload: &Value) -> Result<Value> {
    let relative = payload["relativePath"].as_str().context("缺少相对路径")?;
    let path = safe_path(root, relative)?;
    verify_file(&path)?;
    let before = fs::read(&path)?;
    let expected = payload["originalSha256"].as_str().context("缺少原文件哈希")?;
    if digest(&before) != expected { bail!("原文件已变更，请重新读取后再生成补丁：{}", path.display()) }
    let new_content = payload["newContent"].as_str().context("缺少新内容")?;
    if new_content.len() > MAX_BYTES as usize { bail!("新内容超过 {} MB 大小限制", MAX_BYTES / 1024 / 1024) }
    let old = String::from_utf8(before).context("仅支持 UTF-8 文本")?;
    Ok(json!({"status":"awaiting_approval", "relativePath":relative, "originalSha256":expected, "newSha256":digest(new_content.as_bytes()), "preview":{"before":old,"after":new_content}}))
}

fn apply(root: &Root, payload: &Value) -> Result<Value> {
    let relative = payload["relativePath"].as_str().context("缺少相对路径")?;
    let path = safe_path(root, relative)?;
    verify_file(&path)?;
    validate_approval_token(payload).context("审批令牌无效，拒绝写入")?;
    let before = fs::read(&path)?;
    let expected = payload["originalSha256"].as_str().context("缺少原文件哈希")?;
    if digest(&before) != expected { bail!("原文件已变更，拒绝覆盖（请重新生成补丁）：{}", path.display()) }
    let content = payload["newContent"].as_str().context("缺少新内容")?;
    if content.len() > MAX_BYTES as usize { bail!("新内容超过 {} MB 大小限制", MAX_BYTES / 1024 / 1024) }
    let temp = path.with_extension(format!("agent-tmp-{}", uuid::Uuid::new_v4()));
    { let mut file = fs::File::create(&temp)?; file.write_all(content.as_bytes())?; file.sync_all()?; }
    if let Err(error) = fs::rename(&temp, &path) { let _ = fs::remove_file(&temp); return Err(error.into()); }
    Ok(json!({"relativePath":relative,"sha256":digest(content.as_bytes())}))
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum CommandClass { Auto, Approval, Denied }

fn split_command(input: &str) -> Result<Vec<String>> {
    if input.len() > MAX_COMMAND_LEN {
        bail!("命令过长（超过 {MAX_COMMAND_LEN} 字符）");
    }
    if input.contains(['\n', '\r']) {
        bail!("命令不能包含换行");
    }
    // 先剔除双引号内的内容，检查剩余部分是否含 shell 运算符。
    let mut in_quote = false;
    let mut outside = String::new();
    for ch in input.chars() {
        match ch {
            '"' => in_quote = !in_quote,
            _ if !in_quote => outside.push(ch),
            _ => {}
        }
    }
    if in_quote {
        bail!("命令引号未闭合");
    }
    for op in [';', '&', '|', '<', '>', '`', '$', '(', ')', '^'] {
        if outside.contains(op) {
            bail!("命令包含不允许的 shell 运算符：{op}（如需该字符请用双引号包裹）");
        }
    }
    let mut args = Vec::new();
    let mut current = String::new();
    let mut in_quote = false;
    let mut has = false;
    for ch in input.chars() {
        match ch {
            '"' => in_quote = !in_quote,
            ' ' | '\t' if !in_quote => {
                if has {
                    args.push(std::mem::take(&mut current));
                    has = false;
                }
            }
            _ => {
                current.push(ch);
                has = true;
            }
        }
    }
    if has {
        args.push(current);
    }
    if args.is_empty() {
        bail!("命令为空");
    }
    Ok(args)
}

fn program_name(arg: &str) -> &str {
    Path::new(arg)
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or(arg)
}

fn classify_command(args: &[String]) -> (CommandClass, String) {
    let program = program_name(&args[0]).to_ascii_lowercase();
    if DENIED_PROGRAMS.contains(&program.as_str()) {
        return (CommandClass::Denied, format!("禁止执行程序：{program}"));
    }
    if !ALLOWED_PROGRAMS.contains(&program.as_str()) {
        return (
            CommandClass::Denied,
            format!("程序未在白名单内：{program}（允许：{}）", ALLOWED_PROGRAMS.join("、")),
        );
    }
    if VERSION_PROGRAMS.contains(&program.as_str()) {
        let flags: Vec<&str> = args[1..].iter().map(String::as_str).collect();
        if !flags.is_empty() && flags.len() <= 2 && flags.iter().all(|arg| arg.starts_with('-')) {
            return (CommandClass::Auto, String::new());
        }
    }
    if let Some(reason) = long_running_reason(&program, args) {
        return (CommandClass::Denied, reason);
    }
    match program.as_str() {
        "git" => classify_git(args),
        "npm" | "pnpm" | "yarn" => classify_node_pkg(&program, args),
        "cargo" => classify_cargo(args),
        "go" => classify_go(args),
        "dotnet" => classify_dotnet(args),
        "make" => classify_make(args),
        "cmake" => classify_cmake(args),
        "ninja" => classify_ninja(args),
        "mvn" => classify_mvn(args),
        "gradle" => classify_gradle(args),
        "node" => {
            if args[1..].iter().any(|arg| arg == "--test") {
                (CommandClass::Auto, String::new())
            } else {
                (CommandClass::Approval, "node 命令会执行脚本，需要用户批准".into())
            }
        }
        "python" | "py" => classify_python(args),
        "tsc" => {
            if args.iter().any(|arg| arg == "--noEmit" || arg == "--noEmitOnError") {
                (CommandClass::Auto, String::new())
            } else {
                (CommandClass::Approval, "tsc 会编译输出文件，需要用户批准".into())
            }
        }
        "eslint" => {
            if args.iter().any(|arg| arg == "--fix" || arg == "-fix") {
                (CommandClass::Approval, "eslint --fix 会改写代码，需要用户批准".into())
            } else {
                (CommandClass::Auto, String::new())
            }
        }
        "prettier" => {
            if args.iter().any(|arg| arg == "--write" || arg == "-w") {
                (CommandClass::Approval, "prettier --write 会改写代码，需要用户批准".into())
            } else {
                (CommandClass::Auto, String::new())
            }
        }
        "rustc" | "javac" | "gcc" | "g++" | "clang" | "clang++" => {
            (CommandClass::Approval, format!("{program} 会生成编译产物，需要用户批准"))
        }
        "java" => (CommandClass::Approval, "java 会运行程序，需要用户批准".into()),
        _ => {
            if AUTO_LIST_PROGRAMS.contains(&program.as_str()) {
                (CommandClass::Auto, String::new())
            } else {
                (CommandClass::Approval, format!("{program} 命令需要用户批准后才能执行"))
            }
        }
    }
}

fn long_running_reason(program: &str, args: &[String]) -> Option<String> {
    let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    let script = args.get(2).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    let long_running = match program {
        "npm" | "pnpm" | "yarn" => {
            (sub == "run" && LONG_RUNNING_SCRIPTS.contains(&script.as_str()))
                || LONG_RUNNING_SCRIPTS.contains(&sub.as_str())
        }
        "python" | "py" => sub == "-m" && script == "http.server",
        _ => false,
    };
    long_running.then(|| "这是长驻/交互命令（开发服务器、监听等），不会自行结束，请改用其他方式验证功能".into())
}

fn classify_git(args: &[String]) -> (CommandClass, String) {
    let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    if DENIED_GIT_SUBCOMMANDS.contains(&sub.as_str()) {
        return (
            CommandClass::Denied,
            format!("出于安全考虑禁止 git {sub}（涉及不可逆操作或远程推送）"),
        );
    }
    if sub.is_empty() {
        return (CommandClass::Denied, "git 命令缺少子命令".into());
    }
    if sub == "config" {
        if args.iter().any(|arg| arg == "--global" || arg == "--system") {
            return (CommandClass::Denied, "禁止修改全局 git 配置".into());
        }
        if args.iter().any(|arg| arg == "--list" || arg == "-l" || arg == "--get") {
            return (CommandClass::Auto, String::new());
        }
        return (CommandClass::Approval, "git config 可能修改仓库配置，需要用户批准".into());
    }
    if sub == "checkout" || sub == "switch" {
        if args.iter().any(|arg| arg == "--") || args.get(2).map(String::as_str) == Some(".") {
            return (
                CommandClass::Denied,
                format!("git {sub} 会丢弃工作区文件改动，禁止执行；如要保留改动请先提交或暂存"),
            );
        }
        return (CommandClass::Approval, format!("git {sub} 会切换分支/提交，需要用户批准"));
    }
    if sub == "stash" {
        if args.get(2).map(String::as_str) == Some("list") {
            return (CommandClass::Auto, String::new());
        }
        return (CommandClass::Approval, "git stash 会移动工作区改动，需要用户批准".into());
    }
    if sub == "tag" {
        if args.len() <= 2 {
            return (CommandClass::Auto, String::new());
        }
        return (CommandClass::Approval, "git tag 会创建或删除标签，需要用户批准".into());
    }
    if AUTO_GIT_SUBCOMMANDS.contains(&sub.as_str()) {
        return (CommandClass::Auto, String::new());
    }
    if APPROVAL_GIT_SUBCOMMANDS.contains(&sub.as_str()) {
        return (CommandClass::Approval, format!("git {sub} 会修改工程状态或联网，需要用户批准"));
    }
    (CommandClass::Approval, format!("git {sub} 命令需要用户批准"))
}

fn classify_node_pkg(program: &str, args: &[String]) -> (CommandClass, String) {
    let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    if NPM_PUBLISH_SUBCOMMANDS.contains(&sub.as_str()) {
        return (CommandClass::Denied, format!("禁止 {program} {sub}（涉及账号或远程发布）"));
    }
    if NPM_NETWORK_SUBCOMMANDS.contains(&sub.as_str()) {
        return (
            CommandClass::Approval,
            format!("{program} {sub} 会联网下载或修改依赖，需要用户批准"),
        );
    }
    if sub == "run" {
        let script = args.get(2).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
        if AUTO_BUILD_TEST_SCRIPTS.contains(&script.as_str()) {
            return (CommandClass::Auto, String::new());
        }
        return (CommandClass::Approval, format!("{program} run {script} 会执行项目脚本，需要用户批准"));
    }
    if AUTO_BUILD_TEST_SCRIPTS.contains(&sub.as_str()) {
        return (CommandClass::Auto, String::new());
    }
    if sub == "ls" {
        return (CommandClass::Auto, String::new());
    }
    (CommandClass::Approval, format!("{program} {sub} 命令需要用户批准"))
}

fn classify_cargo(args: &[String]) -> (CommandClass, String) {
    let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    if CARGO_DENIED_SUBCOMMANDS.contains(&sub.as_str()) {
        return (CommandClass::Denied, format!("禁止 cargo {sub}（涉及全局安装或远程发布）"));
    }
    if CARGO_NETWORK_SUBCOMMANDS.contains(&sub.as_str()) {
        return (CommandClass::Approval, format!("cargo {sub} 会联网解析或修改依赖，需要用户批准"));
    }
    match sub.as_str() {
        "test" | "build" | "check" | "clippy" | "bench" => (CommandClass::Auto, String::new()),
        "fmt" => {
            if args.iter().any(|arg| arg == "--check") {
                (CommandClass::Auto, String::new())
            } else {
                (CommandClass::Approval, "cargo fmt 会改写代码，需要用户批准".into())
            }
        }
        "metadata" | "tree" => (CommandClass::Auto, String::new()),
        "run" => (CommandClass::Approval, "cargo run 会启动程序，若 60 秒未结束将被终止；确需运行请批准".into()),
        _ => (CommandClass::Approval, format!("cargo {sub} 命令需要用户批准")),
    }
}

fn classify_go(args: &[String]) -> (CommandClass, String) {
    let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    match sub.as_str() {
        "build" | "test" | "vet" => (CommandClass::Auto, String::new()),
        "fmt" => (CommandClass::Approval, "go fmt 会改写代码，需要用户批准".into()),
        "run" => (CommandClass::Approval, "go run 会启动程序，若 60 秒未结束将被终止；确需运行请批准".into()),
        _ => (CommandClass::Approval, format!("go {sub} 命令需要用户批准（go.mod 相关操作会联网）")),
    }
}

fn classify_dotnet(args: &[String]) -> (CommandClass, String) {
    let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    match sub.as_str() {
        "build" | "test" => (CommandClass::Auto, String::new()),
        "run" => (CommandClass::Approval, "dotnet run 会启动程序，若 60 秒未结束将被终止；确需运行请批准".into()),
        _ => (CommandClass::Approval, format!("dotnet {sub} 命令需要用户批准（add package 会联网）")),
    }
}

fn classify_make(args: &[String]) -> (CommandClass, String) {
    let target = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    if target == "install" {
        return (CommandClass::Denied, "make install 会安装到系统路径，禁止执行".into());
    }
    match target.as_str() {
        "test" | "build" | "check" | "all" => (CommandClass::Auto, String::new()),
        _ => (CommandClass::Approval, format!("make {target} 会执行构建规则，需要用户批准")),
    }
}

fn classify_cmake(args: &[String]) -> (CommandClass, String) {
    if args.iter().any(|arg| arg == "--build") {
        return (CommandClass::Auto, String::new());
    }
    if args.iter().any(|arg| arg == "--install") {
        return (CommandClass::Approval, "cmake --install 会安装到指定前缀，需要用户批准".into());
    }
    (CommandClass::Approval, "cmake 配置/生成命令需要用户批准（会写入构建目录）".into())
}

fn classify_ninja(args: &[String]) -> (CommandClass, String) {
    let target = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    match target.as_str() {
        "" | "test" | "check" => (CommandClass::Auto, String::new()),
        _ => (CommandClass::Approval, format!("ninja {target} 需要用户批准")),
    }
}

fn classify_mvn(args: &[String]) -> (CommandClass, String) {
    let goal = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    match goal.as_str() {
        "compile" | "test" | "package" | "verify" | "clean" => (CommandClass::Auto, String::new()),
        "install" => (CommandClass::Approval, "mvn install 会写入本地仓库，需要用户批准".into()),
        "deploy" => (CommandClass::Denied, "mvn deploy 会发布到远程仓库，禁止执行".into()),
        _ => (CommandClass::Approval, format!("mvn {goal} 命令需要用户批准")),
    }
}

fn classify_gradle(args: &[String]) -> (CommandClass, String) {
    let task = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
    match task.as_str() {
        "build" | "test" | "check" => (CommandClass::Auto, String::new()),
        _ => (CommandClass::Approval, format!("gradle {task} 任务需要用户批准")),
    }
}

fn classify_python(args: &[String]) -> (CommandClass, String) {
    if args.get(1).map(String::as_str) == Some("-m") {
        let module = args.get(2).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
        return match module.as_str() {
            "pytest" | "unittest" => (CommandClass::Auto, String::new()),
            _ => (CommandClass::Approval, format!("python -m {module} 需要用户批准")),
        };
    }
    (CommandClass::Approval, "python 命令会执行脚本，需要用户批准".into())
}

async fn read_capped<R>(mut reader: R, cap: usize) -> (Vec<u8>, bool)
where
    R: AsyncRead + Unpin,
{
    let mut output = Vec::new();
    let mut buffer = [0u8; 8192];
    let mut truncated = false;
    loop {
        match reader.read(&mut buffer).await {
            Ok(0) | Err(_) => break,
            Ok(size) => {
                if output.len() + size >= cap {
                    output.extend_from_slice(&buffer[..cap - output.len()]);
                    truncated = true;
                    // 继续排空管道，避免子进程因输出缓冲区写满而阻塞，直到进程退出。
                    let mut sink = [0u8; 8192];
                    while let Ok(count) = reader.read(&mut sink).await {
                        if count == 0 {
                            break;
                        }
                    }
                    break;
                }
                output.extend_from_slice(&buffer[..size]);
            }
        }
    }
    (output, truncated)
}

#[cfg(windows)]
fn kill_process_tree(pid: u32) {
    let _ = std::process::Command::new("taskkill").args(["/PID", &pid.to_string(), "/T", "/F"]).spawn();
}

#[cfg(not(windows))]
fn kill_process_tree(_pid: u32) {}

async fn run_command(root: &Root, payload: &Value) -> Result<Value> {
    let command = payload["command"].as_str().context("缺少命令内容")?;
    let cwd_relative = payload["cwd"].as_str().unwrap_or("");
    let args = split_command(command)?;
    let (class, reason) = classify_command(&args);
    if class == CommandClass::Denied {
        bail!("{reason}");
    }
    let token_ok = match (payload.get("approvalToken"), payload.get("approvalTokenHash")) {
        (Some(token), Some(hash)) => {
            let token_value = json!({ "approvalToken": token, "approvalTokenHash": hash });
            validate_approval_token(&token_value).context("审批令牌无效，拒绝执行命令")?;
            true
        }
        _ => false,
    };
    if class == CommandClass::Approval && !token_ok {
        let timeout_seconds = approval_timeout_seconds(payload);
        return Ok(json!({"status":"awaiting_approval","command":command,"cwd":cwd_relative,"reason":reason,"timeoutSeconds":timeout_seconds}));
    }
    let timeout_seconds = if class == CommandClass::Auto {
        DEFAULT_COMMAND_TIMEOUT.as_secs()
    } else {
        approval_timeout_seconds(payload)
    };
    let timeout = Duration::from_secs(timeout_seconds);
    let cwd = if cwd_relative.is_empty() {
        root.path.clone()
    } else {
        safe_path(root, cwd_relative)?
    };
    if !cwd.is_dir() {
        bail!("工作目录不是文件夹：{}", cwd.display());
    }
    let start = std::time::Instant::now();
    let mut child = AsyncCommand::new(&args[0])
        .args(&args[1..])
        .current_dir(&cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context(format!("启动命令失败（程序可能未安装）：{}", command))?;
    let pid = child.id().unwrap_or(0);
    let stdout: ChildStdout = child.stdout.take().context("无法捕获标准输出")?;
    let stderr: ChildStderr = child.stderr.take().context("无法捕获标准错误")?;
    let stdout_task = tokio::spawn(read_capped(stdout, MAX_OUTPUT_BYTES));
    let stderr_task = tokio::spawn(read_capped(stderr, MAX_OUTPUT_BYTES));
    let waited = tokio::time::timeout(timeout, child.wait()).await;
    let exit_code = match waited {
        Ok(Ok(status)) => status.code().unwrap_or(-1),
        Ok(Err(error)) => return Err(error.into()),
        Err(_) => {
            let _ = child.kill().await;
            if pid != 0 {
                kill_process_tree(pid);
            }
            bail!("命令执行超过 {timeout_seconds} 秒，已强制终止：{command}");
        }
    };
    let (stdout_bytes, stdout_truncated) = stdout_task.await.context("读取命令输出失败")?;
    let (stderr_bytes, stderr_truncated) = stderr_task.await.context("读取命令错误输出失败")?;
    Ok(json!({
        "status": "completed",
        "command": command,
        "cwd": cwd_relative,
        "exitCode": exit_code,
        "stdout": String::from_utf8_lossy(&stdout_bytes).into_owned(),
        "stderr": String::from_utf8_lossy(&stderr_bytes).into_owned(),
        "truncated": stdout_truncated || stderr_truncated,
        "durationMs": start.elapsed().as_millis() as u64,
        "timeoutSeconds": timeout_seconds,
    }))
}

fn approval_timeout_seconds(payload: &Value) -> u64 {
    payload["timeoutSeconds"]
        .as_u64()
        .unwrap_or(DEFAULT_COMMAND_TIMEOUT.as_secs())
        .clamp(1, MAX_COMMAND_TIMEOUT_SECS)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    #[test] fn rejects_escape() { let dir=std::env::temp_dir().join(format!("agent-test-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); let root=Root{id:"root".into(),path:dir.clone(),label:"test".into()}; assert!(safe_path(&root,"../secret.c").is_err()); fs::remove_dir_all(dir).unwrap(); }
    #[test] fn rejects_secret_file() { let dir=std::env::temp_dir().join(format!("agent-secret-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); let path=dir.join(".env"); fs::write(&path,"SECRET").unwrap(); assert!(verify_file(&path).is_err()); fs::remove_dir_all(dir).unwrap(); }
    #[test] fn allows_common_source_files() { let dir=std::env::temp_dir().join(format!("agent-ext-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); for name in ["main.rs","App.tsx","index.js","script.py","style.css","data.json"] { let path=dir.join(name); fs::write(&path,"test").unwrap(); assert!(verify_file(&path).is_ok(), "应允许读取 {name}"); } fs::remove_dir_all(dir).unwrap(); }
    #[test] fn rejects_oversize_file() { let dir=std::env::temp_dir().join(format!("agent-size-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); let path=dir.join("big.bin"); fs::write(&path,vec![0u8;(MAX_BYTES as usize)+1]).unwrap(); assert!(verify_file(&path).is_err()); fs::remove_dir_all(dir).unwrap(); }
    #[test] fn apply_requires_valid_approval_token() { let dir=std::env::temp_dir().join(format!("agent-apply-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); let path=dir.join("main.c"); fs::write(&path,"int a;").unwrap(); let root=Root{id:"root".into(),path:dir.clone(),label:"test".into()}; let original=digest(&fs::read(&path).unwrap()); let token="approve-token-123456"; let hash=digest(token.as_bytes()); let ok_payload=json!({"kind":"apply_patch","rootId":"root","relativePath":"main.c","originalSha256":original,"newContent":"int b;","approvalToken":token,"approvalTokenHash":hash}); assert!(apply(&root,&ok_payload).is_ok(), "有效令牌应允许写入"); assert_eq!(fs::read_to_string(&path).unwrap(),"int b;"); let bad_payload=json!({"kind":"apply_patch","rootId":"root","relativePath":"main.c","originalSha256":digest(b"int b;"),"newContent":"int c;","approvalToken":"wrong-token","approvalTokenHash":hash}); assert!(apply(&root,&bad_payload).is_err(), "错误令牌应被拒绝"); let missing_payload=json!({"kind":"apply_patch","rootId":"root","relativePath":"main.c","originalSha256":digest(b"int b;"),"newContent":"int c;"}); assert!(apply(&root,&missing_payload).is_err(), "缺少令牌应被拒绝"); fs::remove_dir_all(dir).unwrap(); }
    #[test] fn splits_command_and_rejects_shell_operators() {
        assert_eq!(split_command("git status --short").unwrap(), vec!["git", "status", "--short"]);
        assert_eq!(split_command("rg \"TODO|FIXME\" src").unwrap(), vec!["rg", "TODO|FIXME", "src"]);
        assert_eq!(split_command("git show \"my file.txt\"").unwrap(), vec!["git", "show", "my file.txt"]);
        assert!(split_command("git status; rm -rf .").is_err());
        assert!(split_command("npm test && npm run build").is_err());
        assert!(split_command("git log --format=\"%h\" | head").is_err());
        assert!(split_command("git log \"未闭合").is_err());
        assert!(split_command("").is_err());
    }
    #[test] fn classifies_commands() {
        let class = |command: &str| { let args = split_command(command).unwrap(); classify_command(&args).0 };
        // 只读自动
        assert_eq!(class("git status"), CommandClass::Auto);
        assert_eq!(class("git diff HEAD"), CommandClass::Auto);
        assert_eq!(class("git log --oneline"), CommandClass::Auto);
        assert_eq!(class("git --version"), CommandClass::Auto);
        assert_eq!(class("git config --list"), CommandClass::Auto);
        assert_eq!(class("git stash list"), CommandClass::Auto);
        assert_eq!(class("git tag"), CommandClass::Auto);
        assert_eq!(class("node --version"), CommandClass::Auto);
        assert_eq!(class("rg main src"), CommandClass::Auto);
        assert_eq!(class("npm ls"), CommandClass::Auto);
        // 构建/测试自动
        assert_eq!(class("npm test"), CommandClass::Auto);
        assert_eq!(class("npm run build"), CommandClass::Auto);
        assert_eq!(class("npm run lint"), CommandClass::Auto);
        assert_eq!(class("cargo test"), CommandClass::Auto);
        assert_eq!(class("cargo build"), CommandClass::Auto);
        assert_eq!(class("cargo check"), CommandClass::Auto);
        assert_eq!(class("cargo clippy"), CommandClass::Auto);
        assert_eq!(class("cargo fmt --check"), CommandClass::Auto);
        assert_eq!(class("go build"), CommandClass::Auto);
        assert_eq!(class("go test"), CommandClass::Auto);
        assert_eq!(class("dotnet build"), CommandClass::Auto);
        assert_eq!(class("dotnet test"), CommandClass::Auto);
        assert_eq!(class("make test"), CommandClass::Auto);
        assert_eq!(class("cmake --build build"), CommandClass::Auto);
        assert_eq!(class("ninja"), CommandClass::Auto);
        assert_eq!(class("ninja test"), CommandClass::Auto);
        assert_eq!(class("mvn test"), CommandClass::Auto);
        assert_eq!(class("gradle build"), CommandClass::Auto);
        assert_eq!(class("node --test"), CommandClass::Auto);
        assert_eq!(class("python -m pytest"), CommandClass::Auto);
        assert_eq!(class("python -m unittest"), CommandClass::Auto);
        assert_eq!(class("tsc --noEmit"), CommandClass::Auto);
        assert_eq!(class("eslint src"), CommandClass::Auto);
        assert_eq!(class("prettier --check src"), CommandClass::Auto);
        // 需要审批
        assert_eq!(class("git add src/main.rs"), CommandClass::Approval);
        assert_eq!(class("git commit -m test"), CommandClass::Approval);
        assert_eq!(class("git checkout main"), CommandClass::Approval);
        assert_eq!(class("git fetch"), CommandClass::Approval);
        assert_eq!(class("git pull"), CommandClass::Approval);
        assert_eq!(class("git merge main"), CommandClass::Approval);
        assert_eq!(class("git tag v1.0"), CommandClass::Approval);
        assert_eq!(class("git stash"), CommandClass::Approval);
        assert_eq!(class("git config core.autocrlf"), CommandClass::Approval);
        assert_eq!(class("npm install"), CommandClass::Approval);
        assert_eq!(class("npm run storybook"), CommandClass::Denied);
        assert_eq!(class("pnpm install"), CommandClass::Approval);
        assert_eq!(class("cargo run"), CommandClass::Approval);
        assert_eq!(class("cargo fmt"), CommandClass::Approval);
        assert_eq!(class("cargo update"), CommandClass::Approval);
        assert_eq!(class("go run main.go"), CommandClass::Approval);
        assert_eq!(class("go fmt ./..."), CommandClass::Approval);
        assert_eq!(class("dotnet run"), CommandClass::Approval);
        assert_eq!(class("make"), CommandClass::Approval);
        assert_eq!(class("cmake --install ."), CommandClass::Approval);
        assert_eq!(class("cmake -S . -B build"), CommandClass::Approval);
        assert_eq!(class("mvn install"), CommandClass::Approval);
        assert_eq!(class("node script.js"), CommandClass::Approval);
        assert_eq!(class("python script.py"), CommandClass::Approval);
        assert_eq!(class("tsc"), CommandClass::Approval);
        assert_eq!(class("eslint --fix src"), CommandClass::Approval);
        assert_eq!(class("prettier --write src"), CommandClass::Approval);
        assert_eq!(class("gcc main.c"), CommandClass::Approval);
        // 禁止
        assert_eq!(class("git reset --hard"), CommandClass::Denied);
        assert_eq!(class("git push origin main"), CommandClass::Denied);
        assert_eq!(class("git clone https://example.com/repo"), CommandClass::Denied);
        assert_eq!(class("git checkout -- src/main.c"), CommandClass::Denied);
        assert_eq!(class("git checkout ."), CommandClass::Denied);
        assert_eq!(class("git restore src/main.c"), CommandClass::Denied);
        assert_eq!(class("git clean -fd"), CommandClass::Denied);
        assert_eq!(class("git config --global user.name x"), CommandClass::Denied);
        assert_eq!(class("rm -rf ."), CommandClass::Denied);
        assert_eq!(class("cmd /c dir"), CommandClass::Denied);
        assert_eq!(class("curl https://example.com"), CommandClass::Denied);
        assert_eq!(class("npm publish"), CommandClass::Denied);
        assert_eq!(class("npm run dev"), CommandClass::Denied);
        assert_eq!(class("npm start"), CommandClass::Denied);
        assert_eq!(class("npx prettier"), CommandClass::Denied);
        assert_eq!(class("cargo install clippy"), CommandClass::Denied);
        assert_eq!(class("cargo publish"), CommandClass::Denied);
        assert_eq!(class("mvn deploy"), CommandClass::Denied);
        assert_eq!(class("make install"), CommandClass::Denied);
        assert_eq!(class("python -m http.server"), CommandClass::Denied);
        assert_eq!(class("powershell -Command dir"), CommandClass::Denied);
        assert_eq!(class("docker ps"), CommandClass::Denied);
        assert_eq!(class("unknown-tool --help"), CommandClass::Denied);
    }
    #[tokio::test]
    async fn run_command_requires_approval_without_token() {
        let dir = std::env::temp_dir().join(format!("agent-cmd-approval-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let root = Root { id: "root".into(), path: dir.clone(), label: "test".into() };
        let payload = json!({"kind":"run_command","rootId":"root","command":"npm install","cwd":""});
        let result = run_command(&root, &payload).await.unwrap();
        assert_eq!(result["status"], "awaiting_approval");
        assert_eq!(result["command"], "npm install");
        assert!(result["reason"].as_str().unwrap().contains("批准"));
        assert_eq!(result["timeoutSeconds"], 60);
        fs::remove_dir_all(dir).unwrap();
    }
    #[tokio::test]
    async fn run_command_rejects_denied_and_bad_token() {
        let dir = std::env::temp_dir().join(format!("agent-cmd-deny-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let root = Root { id: "root".into(), path: dir.clone(), label: "test".into() };
        let denied = json!({"kind":"run_command","rootId":"root","command":"rm -rf ."});
        assert!(run_command(&root, &denied).await.is_err(), "删除命令应被拒绝");
        let bad_token = json!({"kind":"run_command","rootId":"root","command":"npm install","approvalToken":"wrong-token-value","approvalTokenHash":"a".repeat(64)});
        assert!(run_command(&root, &bad_token).await.is_err(), "错误审批令牌应被拒绝");
        fs::remove_dir_all(dir).unwrap();
    }
    #[tokio::test]
    async fn run_command_executes_auto_command() {
        if std::process::Command::new("git").arg("--version").output().is_err() { return; }
        let dir = std::env::temp_dir().join(format!("agent-cmd-auto-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let root = Root { id: "root".into(), path: dir.clone(), label: "test".into() };
        let payload = json!({"kind":"run_command","rootId":"root","command":"git --version"});
        let result = run_command(&root, &payload).await.unwrap();
        assert_eq!(result["status"], "completed");
        assert_eq!(result["exitCode"], 0);
        assert!(result["stdout"].as_str().unwrap().contains("git"));
        fs::remove_dir_all(dir).unwrap();
    }
    #[tokio::test]
    async fn run_command_executes_with_valid_approval_token() {
        if std::process::Command::new("npm").arg("--version").output().is_err() { return; }
        let dir = std::env::temp_dir().join(format!("agent-cmd-token-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let root = Root { id: "root".into(), path: dir.clone(), label: "test".into() };
        let token = "approve-command-token-123456";
        let payload = json!({"kind":"run_command","rootId":"root","command":"npm install","cwd":"","approvalToken":token,"approvalTokenHash":digest(token.as_bytes()),"timeoutSeconds":120});
        let result = run_command(&root, &payload).await.unwrap();
        assert_eq!(result["status"], "completed", "带有效令牌的命令应执行：{result}");
        assert!(result["exitCode"].is_number());
        assert_eq!(result["timeoutSeconds"], 120);
        fs::remove_dir_all(dir).unwrap();
    }
}
