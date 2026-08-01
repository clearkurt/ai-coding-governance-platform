use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, io::Write, path::{Component, Path, PathBuf}};

use crate::Root;

const MAX_BYTES: u64 = 4 * 1024 * 1024;
const BLOCKED_NAMES: &[&str] = &[".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"];

fn digest(value: &[u8]) -> String { format!("{:x}", Sha256::digest(value)) }

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

pub fn execute(roots: &[Root], payload: &Value) -> Result<Value> {
    let kind = payload["kind"].as_str().context("缺少任务类型")?;
    let root = root(roots, payload["rootId"].as_str().context("缺少 rootId")?)?;
    match kind { "list_files" => list(root, payload), "read_file" => read(root, payload), "stage_patch" => stage(root, payload), "apply_patch" => apply(root, payload), _ => bail!("不支持的任务") }
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    #[test] fn rejects_escape() { let dir=std::env::temp_dir().join(format!("agent-test-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); let root=Root{id:"root".into(),path:dir.clone(),label:"test".into()}; assert!(safe_path(&root,"../secret.c").is_err()); fs::remove_dir_all(dir).unwrap(); }
    #[test] fn rejects_secret_file() { let dir=std::env::temp_dir().join(format!("agent-secret-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); let path=dir.join(".env"); fs::write(&path,"SECRET").unwrap(); assert!(verify_file(&path).is_err()); fs::remove_dir_all(dir).unwrap(); }
    #[test] fn allows_common_source_files() { let dir=std::env::temp_dir().join(format!("agent-ext-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); for name in ["main.rs","App.tsx","index.js","script.py","style.css","data.json"] { let path=dir.join(name); fs::write(&path,"test").unwrap(); assert!(verify_file(&path).is_ok(), "应允许读取 {name}"); } fs::remove_dir_all(dir).unwrap(); }
    #[test] fn rejects_oversize_file() { let dir=std::env::temp_dir().join(format!("agent-size-{}",uuid::Uuid::new_v4())); fs::create_dir_all(&dir).unwrap(); let path=dir.join("big.bin"); fs::write(&path,vec![0u8;(MAX_BYTES as usize)+1]).unwrap(); assert!(verify_file(&path).is_err()); fs::remove_dir_all(dir).unwrap(); }
}
