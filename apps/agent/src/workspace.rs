use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Component, Path, PathBuf},
};

const MAX_FILES: usize = 10_000;
const MAX_FILE_BYTES: u64 = 20 * 1024 * 1024;
const MAX_TOTAL_BYTES: u64 = 100 * 1024 * 1024;
const EXCLUDED_DIRS: &[&str] = &[
    ".git",
    ".ssh",
    ".gnupg",
    "credentials",
    "auth",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".codex",
    "node_modules",
    "target",
    ".venv",
    "venv",
    "dist",
    "build",
    ".cache",
    "coverage",
];
const SENSITIVE_NAMES: &[&str] = &[
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "auth.json",
    ".npmrc",
    ".pypirc",
    "known_hosts",
];
const SENSITIVE_EXTENSIONS: &[&str] =
    &["pem", "key", "p12", "pfx", "jks", "keystore", "crt", "cer"];

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileRecord {
    pub path: PathBuf,
    pub size: u64,
    pub sha256: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ShadowManifest {
    pub task_id: String,
    pub root: PathBuf,
    pub files: Vec<FileRecord>,
}
pub struct ShadowWorkspace {
    pub path: PathBuf,
    manifest_path: PathBuf,
    manifest: ShadowManifest,
}

pub fn cleanup_all(workspaces: &mut BTreeMap<String, ShadowWorkspace>) -> Result<()> {
    let pending = std::mem::take(workspaces);
    let mut errors = Vec::new();
    for (task_id, workspace) in pending {
        if let Err(error) = workspace.remove() {
            errors.push(format!("{task_id}: {error:#}"));
        }
    }
    if !errors.is_empty() {
        bail!(
            "failed to clean task shadow workspaces: {}",
            errors.join("; ")
        )
    }
    Ok(())
}

impl ShadowWorkspace {
    pub fn real_root(&self) -> &Path {
        &self.manifest.root
    }
    pub fn create(base: &Path, task_id: &str, canonical_root: &Path) -> Result<Self> {
        ensure_real_root(canonical_root)?;
        fs::create_dir_all(base)?;
        let final_path = base.join(format!("{:x}", Sha256::digest(task_id.as_bytes())));
        let manifest_path = base.join(format!(
            "{:x}.manifest.json",
            Sha256::digest(task_id.as_bytes())
        ));
        if final_path.exists() {
            fs::remove_dir_all(&final_path).context("failed to remove stale shadow workspace")?;
        }
        if manifest_path.exists() {
            fs::remove_file(&manifest_path)?;
        }
        let temporary = base.join(format!(".tmp-{}", uuid::Uuid::new_v4()));
        let temporary_manifest = base.join(format!(".manifest-{}.tmp", uuid::Uuid::new_v4()));
        fs::create_dir(&temporary)?;
        let result = (|| {
            let files = copy_filtered(canonical_root, &temporary)?;
            let manifest = ShadowManifest {
                task_id: task_id.into(),
                root: canonical_root.into(),
                files,
            };
            fs::write(&temporary_manifest, serde_json::to_vec_pretty(&manifest)?)?;
            fs::rename(&temporary, &final_path)?;
            fs::rename(&temporary_manifest, &manifest_path)?;
            Ok(Self {
                path: final_path.clone(),
                manifest_path: manifest_path.clone(),
                manifest,
            })
        })();
        if result.is_err() {
            let _ = fs::remove_dir_all(temporary);
            let _ = fs::remove_dir_all(final_path);
            let _ = fs::remove_file(temporary_manifest);
            let _ = fs::remove_file(manifest_path);
        }
        result
    }
    pub fn sync_back(&self, canonical_root: &Path) -> Result<()> {
        self.sync_back_inner(canonical_root, None)
    }
    fn sync_back_inner(&self, canonical_root: &Path, fail_after: Option<usize>) -> Result<()> {
        ensure_real_root(canonical_root)?;
        if self.manifest.root != canonical_root {
            bail!("shadow workspace root mismatch")
        }
        ensure_tree_root(&self.path)?;
        let original: BTreeMap<_, _> = self
            .manifest
            .files
            .iter()
            .map(|f| (f.path.clone(), f.clone()))
            .collect();
        let shadow = scan_filtered(&self.path, true)?;
        let current = scan_filtered(canonical_root, false)?;
        let shadow_map: BTreeMap<_, _> = shadow.into_iter().map(|f| (f.path.clone(), f)).collect();
        let current_map: BTreeMap<_, _> =
            current.into_iter().map(|f| (f.path.clone(), f)).collect();
        let mut changes = Vec::new();
        for (path, before) in &original {
            match shadow_map.get(path) {
                Some(after) if after.sha256 != before.sha256 => {
                    changes.push(Change::Write(path.clone()))
                }
                None => changes.push(Change::Delete(path.clone())),
                _ => {}
            }
        }
        for path in shadow_map.keys().filter(|p| !original.contains_key(*p)) {
            changes.push(Change::Write(path.clone()));
        }
        for change in &changes {
            let path = change.path();
            safe_relative(path)?;
            validate_parents(canonical_root, path)?;
            match original.get(path) {
                Some(before) => match current_map.get(path) {
                    Some(now) if now.sha256 == before.sha256 => {}
                    _ => bail!("concurrent user modification detected: {}", path.display()),
                },
                None if current_map.contains_key(path) => {
                    bail!("concurrent user file creation detected: {}", path.display())
                }
                None => {}
            }
        }
        let transaction = self
            .path
            .parent()
            .context("shadow path has no parent")?
            .join(format!(".sync-{}", uuid::Uuid::new_v4()));
        let staged = transaction.join("staged");
        let recovery = transaction.join("recovery");
        fs::create_dir_all(&staged)?;
        fs::create_dir_all(&recovery)?;
        for change in &changes {
            if let Change::Write(path) = change {
                validate_parents(&self.path, path)?;
                let source = self.path.join(path);
                if is_reparse(&fs::symlink_metadata(&source)?) || !source.is_file() {
                    bail!("shadow source changed into an unsafe file type")
                }
                let target = staged.join(path);
                if let Some(parent) = target.parent() {
                    fs::create_dir_all(parent)?
                };
                fs::copy(source, target)?;
            }
        }
        let mut applied: Vec<(PathBuf, bool)> = Vec::new();
        let apply = (|| {
            for (index, change) in changes.iter().enumerate() {
                if fail_after == Some(index) {
                    bail!("injected sync failure")
                };
                let relative = change.path();
                validate_parents(canonical_root, relative)?;
                let target = canonical_root.join(relative);
                let existed = target.exists();
                match original.get(relative) {
                    Some(before) if !existed || hash_file(&target)? != before.sha256 => {
                        bail!(
                            "concurrent user modification detected during apply: {}",
                            relative.display()
                        )
                    }
                    None if existed => bail!(
                        "concurrent user file creation detected during apply: {}",
                        relative.display()
                    ),
                    _ => {}
                }
                if existed {
                    let saved = recovery.join(relative);
                    if let Some(parent) = saved.parent() {
                        fs::create_dir_all(parent)?
                    };
                    fs::rename(&target, &saved)?;
                }
                applied.push((relative.to_path_buf(), existed));
                if let Change::Write(_) = change {
                    let source = staged.join(relative);
                    if let Some(parent) = target.parent() {
                        fs::create_dir_all(parent)?
                    };
                    fs::rename(source, &target)?;
                }
            }
            Ok(())
        })();
        if let Err(error) = apply {
            let mut recovery_errors = Vec::new();
            for (relative, existed) in applied.iter().rev() {
                let target = canonical_root.join(relative);
                if target.exists() {
                    if let Err(restore_error) = fs::remove_file(&target) {
                        recovery_errors.push(restore_error.to_string());
                    }
                }
                if *existed {
                    let saved = recovery.join(relative);
                    if let Some(parent) = target.parent() {
                        let _ = fs::create_dir_all(parent);
                    }
                    if let Err(restore_error) = fs::rename(saved, target) {
                        recovery_errors.push(restore_error.to_string());
                    }
                }
            }
            let _ = fs::remove_dir_all(&transaction);
            if recovery_errors.is_empty() {
                return Err(error.context("shadow sync failed; applied changes were restored"));
            }
            return Err(error.context(format!(
                "shadow sync failed and recovery had errors: {}",
                recovery_errors.join("; ")
            )));
        }
        let _ = fs::remove_dir_all(transaction);
        Ok(())
    }
    pub fn remove(self) -> Result<()> {
        fs::remove_dir_all(self.path)?;
        if self.manifest_path.exists() {
            fs::remove_file(self.manifest_path)?;
        }
        Ok(())
    }
}
#[derive(Clone)]
enum Change {
    Write(PathBuf),
    Delete(PathBuf),
}
impl Change {
    fn path(&self) -> &Path {
        match self {
            Self::Write(p) | Self::Delete(p) => p,
        }
    }
}

fn copy_filtered(root: &Path, dest: &Path) -> Result<Vec<FileRecord>> {
    let files = scan_filtered(root, false)?;
    for file in &files {
        let source = root.join(&file.path);
        validate_parents(root, &file.path)?;
        if is_reparse(&fs::symlink_metadata(&source)?) || !source.is_file() {
            bail!("source changed into an unsafe file type")
        }
        let target = dest.join(&file.path);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?
        };
        fs::copy(source, target)?;
    }
    Ok(files)
}
fn scan_filtered(root: &Path, ignore_manifest: bool) -> Result<Vec<FileRecord>> {
    let mut files = Vec::new();
    let mut total = 0u64;
    fn visit(
        root: &Path,
        dir: &Path,
        files: &mut Vec<FileRecord>,
        total: &mut u64,
        ignore_manifest: bool,
    ) -> Result<()> {
        for item in fs::read_dir(dir)? {
            let item = item?;
            let path = item.path();
            let rel = path.strip_prefix(root)?;
            safe_relative(rel)?;
            let meta = fs::symlink_metadata(&path)?;
            if is_reparse(&meta) {
                bail!(
                    "refusing symlink, junction, or reparse point: {}",
                    rel.display()
                )
            }
            if meta.is_dir() {
                if excluded_dir(&item.file_name()) {
                    continue;
                }
                visit(root, &path, files, total, ignore_manifest)?;
            } else if meta.is_file() {
                if sensitive_file(&item.file_name())
                    || (ignore_manifest && item.file_name() == ".company-shadow-manifest.json")
                {
                    continue;
                }
                if meta.len() > MAX_FILE_BYTES {
                    bail!(
                        "workspace file exceeds single-file limit: {}",
                        rel.display()
                    )
                }
                *total = total
                    .checked_add(meta.len())
                    .context("workspace size overflow")?;
                if *total > MAX_TOTAL_BYTES || files.len() >= MAX_FILES {
                    bail!("workspace exceeds file-count or total-size limit")
                }
                files.push(FileRecord {
                    path: rel.into(),
                    size: meta.len(),
                    sha256: hash_file(&path)?,
                });
            }
        }
        Ok(())
    }
    visit(root, root, &mut files, &mut total, ignore_manifest)?;
    files.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(files)
}
fn excluded_dir(name: &std::ffi::OsStr) -> bool {
    EXCLUDED_DIRS.iter().any(|v| name.eq_ignore_ascii_case(v))
}
fn sensitive_file(name: &std::ffi::OsStr) -> bool {
    let value = name.to_string_lossy();
    let lower = value.to_ascii_lowercase();
    lower == ".env"
        || lower.starts_with(".env.")
        || SENSITIVE_NAMES.iter().any(|v| lower == *v)
        || Path::new(lower.as_str())
            .extension()
            .and_then(|v| v.to_str())
            .is_some_and(|v| SENSITIVE_EXTENSIONS.contains(&v))
}
fn hash_file(path: &Path) -> Result<String> {
    Ok(format!("{:x}", Sha256::digest(fs::read(path)?)))
}
fn safe_relative(path: &Path) -> Result<()> {
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        bail!("unsafe relative workspace path")
    }
    Ok(())
}
fn is_reparse(meta: &fs::Metadata) -> bool {
    if meta.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        meta.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        false
    }
}
fn ensure_real_root(root: &Path) -> Result<()> {
    if !root.is_absolute()
        || fs::canonicalize(root)? != root
        || is_reparse(&fs::symlink_metadata(root)?)
    {
        bail!("authorized root changed or is a reparse point")
    }
    Ok(())
}
fn ensure_tree_root(root: &Path) -> Result<()> {
    if !root.is_absolute() || !root.is_dir() || is_reparse(&fs::symlink_metadata(root)?) {
        bail!("shadow root is invalid")
    }
    Ok(())
}
fn validate_parents(root: &Path, relative: &Path) -> Result<()> {
    safe_relative(relative)?;
    let mut current = root.to_path_buf();
    for component in relative.parent().into_iter().flat_map(Path::components) {
        let Component::Normal(name) = component else {
            bail!("unsafe path")
        };
        current.push(name);
        if current.exists() && is_reparse(&fs::symlink_metadata(&current)?) {
            bail!("path crosses a reparse point")
        }
    }
    Ok(())
}

pub fn sanitize_event(mut value: Value, shadow: &Path, real: &Path) -> Value {
    sanitize_inner(&mut value, shadow, real);
    value
}
pub fn sanitize_audit(mut value: Value) -> Value {
    sanitize_audit_inner(&mut value);
    value
}
fn sanitize_audit_inner(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, item) in map.iter_mut() {
                let normalized = key.to_ascii_lowercase().replace(['-', '_'], "");
                if [
                    "token",
                    "password",
                    "secret",
                    "apikey",
                    "authorization",
                    "cookie",
                    "credential",
                ]
                .iter()
                .any(|v| normalized.contains(v))
                {
                    *item = Value::String("[REDACTED]".into())
                } else {
                    sanitize_audit_inner(item)
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                sanitize_audit_inner(item)
            }
        }
        Value::String(text) => {
            if contains_secret(text) || text.contains(":\\") || text.starts_with('/') {
                *text = "[REDACTED]".into()
            }
        }
        _ => {}
    }
}
fn sanitize_inner(value: &mut Value, shadow: &Path, real: &Path) {
    match value {
        Value::Object(map) => {
            for (key, item) in map.iter_mut() {
                let normalized = key.to_ascii_lowercase().replace(['-', '_'], "");
                if [
                    "token",
                    "password",
                    "secret",
                    "apikey",
                    "authorization",
                    "cookie",
                    "credential",
                ]
                .iter()
                .any(|v| normalized.contains(v))
                {
                    *item = Value::String("[REDACTED]".into())
                } else {
                    sanitize_inner(item, shadow, real)
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                sanitize_inner(item, shadow, real)
            }
        }
        Value::String(text) => {
            if contains_secret(text) {
                *text = "[REDACTED]".into()
            } else {
                *text = replace_path(text, shadow);
                *text = replace_path(text, real);
            }
        }
        _ => {}
    }
}

#[cfg(windows)]
fn replace_path(text: &str, path: &Path) -> String {
    let needle = path
        .to_string_lossy()
        .replace('\\', "/")
        .to_ascii_lowercase();
    if needle.is_empty() {
        return text.into();
    }
    let mut output = text.to_owned();
    let mut offset = 0;
    loop {
        let normalized = output.replace('\\', "/").to_ascii_lowercase();
        let Some(relative) = normalized[offset..].find(&needle) else {
            break;
        };
        let start = offset + relative;
        output.replace_range(start..start + needle.len(), "$WORKSPACE");
        offset = start + "$WORKSPACE".len();
    }
    output
}

#[cfg(not(windows))]
fn replace_path(text: &str, path: &Path) -> String {
    text.replace(path.to_string_lossy().as_ref(), "$WORKSPACE")
}
fn contains_secret(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    lower.contains("bearer ")
        || lower.contains("-----begin ") && lower.contains("private key-----")
        || lower
            .split(|c: char| c.is_whitespace() || c == '\"' || c == '\'')
            .any(|part| {
                part.starts_with("sk-") || part.starts_with("dk-") || part.starts_with("api_")
            })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (PathBuf, PathBuf, PathBuf) {
        let base = std::env::temp_dir().join(format!("shadow-test-{}", uuid::Uuid::new_v4()));
        let root = base.join("root");
        let shadows = base.join("shadows");
        fs::create_dir_all(&root).unwrap();
        (base, fs::canonicalize(root).unwrap(), shadows)
    }
    #[test]
    fn filters_sensitive_content_and_records_hashes() {
        let (base, root, shadows) = fixture();
        fs::write(root.join("safe.txt"), "safe").unwrap();
        fs::write(root.join(".env.local"), "TOKEN=x").unwrap();
        fs::write(root.join("client.pem"), "private").unwrap();
        fs::create_dir(root.join(".git")).unwrap();
        fs::write(root.join(".git/config"), "secret").unwrap();
        for directory in [
            "credentials",
            "AUTH",
            ".aws",
            ".azure",
            ".kube",
            ".docker",
            ".codex",
        ] {
            fs::create_dir(root.join(directory)).unwrap();
            fs::write(root.join(directory).join("hidden"), "credential").unwrap();
        }
        let ws = ShadowWorkspace::create(&shadows, "t", &root).unwrap();
        assert!(ws.path.join("safe.txt").exists());
        assert!(!ws.path.join(".env.local").exists());
        assert!(!ws.path.join("client.pem").exists());
        for directory in [
            "credentials",
            "AUTH",
            ".aws",
            ".azure",
            ".kube",
            ".docker",
            ".codex",
        ] {
            assert!(!ws.path.join(directory).exists());
        }
        assert!(!ws.path.join(".company-shadow-manifest.json").exists());
        assert!(ws.manifest_path.exists());
        assert_eq!(ws.manifest.files.len(), 1);
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn syncs_add_modify_delete_and_rejects_concurrent_changes() {
        let (base, root, shadows) = fixture();
        fs::write(root.join("modify"), "before").unwrap();
        fs::write(root.join("delete"), "before").unwrap();
        let ws = ShadowWorkspace::create(&shadows, "t", &root).unwrap();
        fs::write(ws.path.join("modify"), "after").unwrap();
        fs::remove_file(ws.path.join("delete")).unwrap();
        fs::write(ws.path.join("new"), "new").unwrap();
        ws.sync_back(&root).unwrap();
        assert_eq!(fs::read_to_string(root.join("modify")).unwrap(), "after");
        assert!(!root.join("delete").exists());
        assert_eq!(fs::read_to_string(root.join("new")).unwrap(), "new");
        let ws2 = ShadowWorkspace::create(&shadows, "t2", &root).unwrap();
        fs::write(ws2.path.join("modify"), "agent").unwrap();
        fs::write(root.join("modify"), "user").unwrap();
        assert!(
            ws2.sync_back(&root)
                .unwrap_err()
                .to_string()
                .contains("concurrent")
        );
        assert_eq!(fs::read_to_string(root.join("modify")).unwrap(), "user");
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn failed_sync_restores_already_applied_changes() {
        let (base, root, shadows) = fixture();
        fs::write(root.join("a"), "old-a").unwrap();
        fs::write(root.join("b"), "old-b").unwrap();
        let ws = ShadowWorkspace::create(&shadows, "t", &root).unwrap();
        fs::write(ws.path.join("a"), "new-a").unwrap();
        fs::write(ws.path.join("b"), "new-b").unwrap();
        assert!(ws.sync_back_inner(&root, Some(1)).is_err());
        assert_eq!(fs::read_to_string(root.join("a")).unwrap(), "old-a");
        assert_eq!(fs::read_to_string(root.join("b")).unwrap(), "old-b");
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn rejects_traversal_and_symlinks_when_supported() {
        assert!(safe_relative(Path::new("../x")).is_err());
        let (base, root, shadows) = fixture();
        let outside = base.join("outside");
        fs::write(&outside, "x").unwrap();
        #[cfg(windows)]
        let linked = std::os::windows::fs::symlink_file(&outside, root.join("link"));
        #[cfg(unix)]
        let linked = std::os::unix::fs::symlink(&outside, root.join("link"));
        if linked.is_ok() {
            assert!(ShadowWorkspace::create(&shadows, "t", &root).is_err())
        }
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn rejects_oversized_shadow_without_publication() {
        let (base, root, shadows) = fixture();
        fs::File::create(root.join("large.bin"))
            .unwrap()
            .set_len(MAX_FILE_BYTES + 1)
            .unwrap();
        assert!(ShadowWorkspace::create(&shadows, "large", &root).is_err());
        let published = shadows.join(format!("{:x}", Sha256::digest(b"large")));
        assert!(!published.exists());
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn redacts_paths_keys_and_secret_shapes() {
        let shadow = Path::new("C:\\shadow");
        let real = Path::new("C:\\real");
        let value = serde_json::json!({"cwd":"C:\\shadow\\src","real":"C:\\real\\a","api_key":"abc","nested":{"message":"Authorization: Bearer xyz"},"private":"-----BEGIN PRIVATE KEY----- x"});
        let clean = sanitize_event(value, shadow, real);
        let text = clean.to_string();
        assert!(!text.contains("C:\\\\shadow"));
        assert!(!text.contains("C:\\\\real"));
        assert!(!text.contains("abc"));
        assert!(!text.contains("xyz"));
        assert!(!text.contains("PRIVATE KEY"));
    }
    #[cfg(windows)]
    #[test]
    fn redacts_windows_path_case_and_separator_variants() {
        let clean = sanitize_event(
            serde_json::json!({"a":"c:/SHADOW/src","b":"C:/REAL/src"}),
            Path::new("C:\\shadow"),
            Path::new("c:\\real"),
        );
        let text = clean.to_string();
        assert!(!text.to_ascii_lowercase().contains("shadow"));
        assert!(!text.to_ascii_lowercase().contains("real"));
        assert_eq!(text.matches("$WORKSPACE").count(), 2);
    }
    #[test]
    fn cleanup_all_removes_shadow_and_manifest() {
        let (base, root, shadows) = fixture();
        fs::write(root.join("safe"), "x").unwrap();
        let workspace = ShadowWorkspace::create(&shadows, "task", &root).unwrap();
        let shadow_path = workspace.path.clone();
        let manifest_path = workspace.manifest_path.clone();
        let mut active = BTreeMap::from([("task".into(), workspace)]);
        cleanup_all(&mut active).unwrap();
        assert!(active.is_empty());
        assert!(!shadow_path.exists());
        assert!(!manifest_path.exists());
        fs::remove_dir_all(base).unwrap();
    }
}
