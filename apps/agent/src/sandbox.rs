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
const COMMAND_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_OUTPUT_BYTES: usize = 256 * 1024;

const ALLOWED_PROGRAMS: &[&str] = &[
    "git", "npm", "node", "cargo", "rustc", "python", "py", "rg", "ls", "dir", "find", "findstr", "pwd", "tree", "where",
];
const DENIED_PROGRAMS: &[&str] = &[
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "wsl", "rm", "del", "erase", "rmdir", "rd", "remove-item", "curl",
    "wget", "iwr", "invoke-webrequest", "ping", "taskkill", "start-process", "reset",
];
const DENIED_GIT_SUBCOMMANDS: &[&str] = &[
    "reset", "clean", "push", "pull", "fetch", "clone", "merge", "rebase", "checkout", "switch", "restore", "rm", "mv",
    "gc", "prune", "filter-branch",
];
const AUTO_GIT_SUBCOMMANDS: &[&str] = &[
    "status", "log", "diff", "show", "branch", "remote", "ls-files", "grep", "rev-parse", "tag", "stash", "describe",
    "shortlog", "blame", "config",
];
const AUTO_LIST_PROGRAMS: &[&str] = &["ls", "dir", "rg", "find", "findstr", "pwd", "tree", "where"];
const VERSION_PROGRAMS: &[&str] = &["git", "npm", "node", "cargo", "rustc", "python", "py"];

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
    if program == "git" {
        let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
        if DENIED_GIT_SUBCOMMANDS.contains(&sub.as_str()) {
            return (CommandClass::Denied, format!("出于安全考虑禁止 git {sub}（涉及网络、删除或重写历史）"));
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
        if AUTO_GIT_SUBCOMMANDS.contains(&sub.as_str()) {
            return (CommandClass::Auto, String::new());
        }
        return (CommandClass::Approval, format!("git {sub} 会修改工程状态，需要用户批准"));
    }
    if program == "npm" {
        let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
        match sub.as_str() {
            "install" | "i" | "add" | "publish" | "uninstall" | "remove" | "update" | "outdated" | "audit" | "ci"
            | "login" | "logout" | "whoami" => return (CommandClass::Denied, format!("禁止 npm {sub}（涉及网络或全局变更）")),
            "ls" => return (CommandClass::Auto, String::new()),
            _ => return (CommandClass::Approval, "npm 命令会执行项目脚本或修改依赖，需要用户批准".into()),
        }
    }
    if program == "cargo" {
        let sub = args.get(1).map(|value| value.to_ascii_lowercase()).unwrap_or_default();
        match sub.as_str() {
            "install" | "publish" | "update" | "search" | "login" | "logout" | "owner" | "add" | "remove" => {
                return (CommandClass::Denied, format!("禁止 cargo {sub}（涉及网络或全局变更）"))
            }
            "metadata" | "tree" => return (CommandClass::Auto, String::new()),
            _ => return (CommandClass::Approval, "cargo 命令会构建或测试工程，需要用户批准".into()),
        }
    }
    if AUTO_LIST_PROGRAMS.contains(&program.as_str()) {
        return (CommandClass::Auto, String::new());
    }
    (CommandClass::Approval, format!("{program} 命令需要用户批准后才能执行"))
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
        return Ok(json!({"status":"awaiting_approval","command":command,"cwd":cwd_relative,"reason":reason}));
    }
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
    let waited = tokio::time::timeout(COMMAND_TIMEOUT, child.wait()).await;
    let exit_code = match waited {
        Ok(Ok(status)) => status.code().unwrap_or(-1),
        Ok(Err(error)) => return Err(error.into()),
        Err(_) => {
            let _ = child.kill().await;
            if pid != 0 {
                kill_process_tree(pid);
            }
            bail!("命令执行超过 60 秒，已强制终止：{command}");
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
    }))
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
        assert_eq!(class("git status"), CommandClass::Auto);
        assert_eq!(class("git diff HEAD"), CommandClass::Auto);
        assert_eq!(class("git log --oneline"), CommandClass::Auto);
        assert_eq!(class("git --version"), CommandClass::Auto);
        assert_eq!(class("git config --list"), CommandClass::Auto);
        assert_eq!(class("node --version"), CommandClass::Auto);
        assert_eq!(class("rg main src"), CommandClass::Auto);
        assert_eq!(class("npm ls"), CommandClass::Auto);
        assert_eq!(class("git add src/main.rs"), CommandClass::Approval);
        assert_eq!(class("git commit -m test"), CommandClass::Approval);
        assert_eq!(class("npm test"), CommandClass::Approval);
        assert_eq!(class("cargo test"), CommandClass::Approval);
        assert_eq!(class("node script.js"), CommandClass::Approval);
        assert_eq!(class("git reset --hard"), CommandClass::Denied);
        assert_eq!(class("git push origin main"), CommandClass::Denied);
        assert_eq!(class("git checkout main"), CommandClass::Denied);
        assert_eq!(class("git config --global user.name x"), CommandClass::Denied);
        assert_eq!(class("rm -rf ."), CommandClass::Denied);
        assert_eq!(class("cmd /c dir"), CommandClass::Denied);
        assert_eq!(class("curl https://example.com"), CommandClass::Denied);
        assert_eq!(class("npm install"), CommandClass::Denied);
        assert_eq!(class("npx prettier"), CommandClass::Denied);
        assert_eq!(class("powershell -Command dir"), CommandClass::Denied);
        assert_eq!(class("python -m http.server"), CommandClass::Approval);
        assert_eq!(class("unknown-tool --help"), CommandClass::Denied);
    }
    #[tokio::test]
    async fn run_command_requires_approval_without_token() {
        let dir = std::env::temp_dir().join(format!("agent-cmd-approval-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let root = Root { id: "root".into(), path: dir.clone(), label: "test".into() };
        let payload = json!({"kind":"run_command","rootId":"root","command":"npm test","cwd":""});
        let result = run_command(&root, &payload).await.unwrap();
        assert_eq!(result["status"], "awaiting_approval");
        assert_eq!(result["command"], "npm test");
        assert!(result["reason"].as_str().unwrap().contains("批准"));
        fs::remove_dir_all(dir).unwrap();
    }
    #[tokio::test]
    async fn run_command_rejects_denied_and_bad_token() {
        let dir = std::env::temp_dir().join(format!("agent-cmd-deny-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let root = Root { id: "root".into(), path: dir.clone(), label: "test".into() };
        let denied = json!({"kind":"run_command","rootId":"root","command":"rm -rf ."});
        assert!(run_command(&root, &denied).await.is_err(), "删除命令应被拒绝");
        let bad_token = json!({"kind":"run_command","rootId":"root","command":"npm test","approvalToken":"wrong-token-value","approvalTokenHash":"a".repeat(64)});
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
        let payload = json!({"kind":"run_command","rootId":"root","command":"npm test","cwd":"","approvalToken":token,"approvalTokenHash":digest(token.as_bytes())});
        let result = run_command(&root, &payload).await.unwrap();
        assert_eq!(result["status"], "completed", "带有效令牌的命令应执行：{result}");
        assert!(result["exitCode"].is_number());
        fs::remove_dir_all(dir).unwrap();
    }
}
