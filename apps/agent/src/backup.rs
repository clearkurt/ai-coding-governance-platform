use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Component, Path, PathBuf},
};

pub const MAX_TOTAL_BYTES: u64 = 100 * 1024 * 1024;
pub const MAX_FILES: usize = 10_000;
pub const MAX_FILE_BYTES: u64 = 20 * 1024 * 1024;
pub const EXCLUDED_DIRS: &[&str] = &[
    ".git",
    "node_modules",
    "target",
    ".venv",
    "venv",
    "dist",
    "build",
];
pub const EXCLUDED_FILES: &[&str] = &[
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "auth.json",
];

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Manifest {
    pub task_id: String,
    pub root_id: String,
    pub root: PathBuf,
    pub limits: Limits,
    pub retention_policy: String,
    pub excluded_directories: Vec<String>,
    #[serde(default)]
    pub excluded_sensitive_files: Vec<String>,
    pub entries: Vec<Entry>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Limits {
    pub total_bytes: u64,
    pub files: usize,
    pub single_file_bytes: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Entry {
    pub path: PathBuf,
    pub kind: EntryKind,
    pub size: u64,
    pub sha256: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EntryKind {
    Directory,
    File,
}

pub struct BackupStore {
    base: PathBuf,
}
impl BackupStore {
    pub fn new(base: PathBuf) -> Result<Self> {
        fs::create_dir_all(&base)?;
        Ok(Self { base })
    }
    fn key(task_id: &str) -> String {
        format!("{:x}", Sha256::digest(task_id.as_bytes()))
    }
    pub fn snapshot_dir(&self, task_id: &str) -> PathBuf {
        self.base.join(Self::key(task_id))
    }
    pub fn create(&self, task_id: &str, root_id: &str, canonical_root: &Path) -> Result<Manifest> {
        ensure_canonical_root(canonical_root)?;
        let final_dir = self.snapshot_dir(task_id);
        if final_dir.exists() {
            let existing = self.load(task_id)?;
            if existing.root != canonical_root || existing.root_id != root_id {
                bail!("existing backup root mismatch")
            }
            return Ok(existing);
        }
        let temporary = self.base.join(format!(".tmp-{}", uuid::Uuid::new_v4()));
        let content = temporary.join("content");
        fs::create_dir_all(&content)?;
        let result = (|| {
            let entries = scan_and_copy(canonical_root, &content, true)?;
            let manifest = Manifest {
                task_id: task_id.into(),
                root_id: root_id.into(),
                root: canonical_root.to_path_buf(),
                limits: Limits {
                    total_bytes: MAX_TOTAL_BYTES,
                    files: MAX_FILES,
                    single_file_bytes: MAX_FILE_BYTES,
                },
                retention_policy: "retained_until_explicit_cleanup".into(),
                excluded_directories: EXCLUDED_DIRS.iter().map(|v| (*v).into()).collect(),
                excluded_sensitive_files: vec![
                    ".env and .env.*".into(),
                    "id_rsa/id_ed25519".into(),
                    "credentials/auth.json".into(),
                ],
                entries,
            };
            fs::write(
                temporary.join("manifest.json"),
                serde_json::to_vec_pretty(&manifest)?,
            )?;
            fs::rename(&temporary, &final_dir)
                .context("failed to atomically publish task backup")?;
            Ok(manifest)
        })();
        if result.is_err() {
            let _ = fs::remove_dir_all(&temporary);
        }
        result
    }
    pub fn load(&self, task_id: &str) -> Result<Manifest> {
        let dir = self.snapshot_dir(task_id);
        let manifest: Manifest = serde_json::from_slice(
            &fs::read(dir.join("manifest.json")).context("task backup not found")?,
        )?;
        if manifest.task_id != task_id {
            bail!("backup task mismatch")
        }
        validate_manifest(&manifest)?;
        Ok(manifest)
    }
    pub fn rollback(
        &self,
        task_id: &str,
        root_id: &str,
        canonical_root: &Path,
    ) -> Result<Manifest> {
        ensure_canonical_root(canonical_root)?;
        let manifest = self.load(task_id)?;
        if manifest.root != canonical_root || manifest.root_id != root_id {
            bail!("backup root mismatch")
        }
        let snapshot = self.snapshot_dir(task_id).join("content");
        let recovery = self
            .base
            .join(format!(".rollback-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&recovery)?;
        let current_entries = scan_and_copy(canonical_root, &recovery, true)
            .context("failed to stage current tree before rollback")?;
        let apply = apply_tree(
            canonical_root,
            &snapshot,
            &manifest.entries,
            &current_entries,
        );
        if let Err(error) = apply {
            let recovery_result = apply_tree(
                canonical_root,
                &recovery,
                &current_entries,
                &scan(canonical_root)?,
            );
            let _ = fs::remove_dir_all(&recovery);
            return match recovery_result {
                Ok(()) => Err(error.context("rollback failed; pre-rollback tree was restored")),
                Err(recovery_error) => Err(error.context(format!(
                    "rollback failed and recovery also failed: {recovery_error:#}"
                ))),
            };
        }
        fs::remove_dir_all(recovery)?;
        Ok(manifest)
    }
}

fn ensure_canonical_root(root: &Path) -> Result<()> {
    if !root.is_absolute() || fs::canonicalize(root)? != root {
        bail!("backup root must remain canonical and unchanged")
    }
    let metadata = fs::symlink_metadata(root)?;
    if is_link_or_reparse(&metadata) {
        bail!("backup root cannot be a symlink, junction, or reparse point")
    }
    Ok(())
}
fn ensure_tree_root(root: &Path) -> Result<()> {
    if !root.is_absolute() || !root.is_dir() || is_link_or_reparse(&fs::symlink_metadata(root)?) {
        bail!("tree source must be an absolute real directory")
    }
    Ok(())
}
fn excluded(name: &std::ffi::OsStr) -> bool {
    EXCLUDED_DIRS.iter().any(|v| name.eq_ignore_ascii_case(v))
}
fn sensitive_file(name: &std::ffi::OsStr) -> bool {
    let value = name.to_string_lossy();
    value.eq_ignore_ascii_case(".env")
        || value.to_ascii_lowercase().starts_with(".env.")
        || EXCLUDED_FILES
            .iter()
            .any(|blocked| value.eq_ignore_ascii_case(blocked))
}
fn is_link_or_reparse(metadata: &fs::Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        false
    }
}
fn safe_relative(path: &Path) -> Result<()> {
    if path.is_absolute()
        || path
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        bail!("manifest contains unsafe relative path")
    }
    Ok(())
}
fn sha(path: &Path) -> Result<String> {
    Ok(format!("{:x}", Sha256::digest(fs::read(path)?)))
}

fn scan(root: &Path) -> Result<Vec<Entry>> {
    scan_impl(root, None)
}
fn scan_and_copy(root: &Path, destination: &Path, enforce_limits: bool) -> Result<Vec<Entry>> {
    scan_impl(root, Some((destination, enforce_limits)))
}
fn scan_impl(root: &Path, copy: Option<(&Path, bool)>) -> Result<Vec<Entry>> {
    let mut entries = Vec::new();
    let mut total = 0u64;
    let mut files = 0usize;
    fn visit(
        root: &Path,
        dir: &Path,
        copy: Option<(&Path, bool)>,
        entries: &mut Vec<Entry>,
        total: &mut u64,
        files: &mut usize,
    ) -> Result<()> {
        for item in fs::read_dir(dir)? {
            let item = item?;
            let path = item.path();
            let relative = path.strip_prefix(root)?;
            safe_relative(relative)?;
            let metadata = fs::symlink_metadata(&path)?;
            if is_link_or_reparse(&metadata) {
                bail!(
                    "refusing symlink, junction, or reparse point: {}",
                    relative.display()
                )
            }
            if metadata.is_dir() {
                if excluded(&item.file_name()) {
                    continue;
                }
                entries.push(Entry {
                    path: relative.into(),
                    kind: EntryKind::Directory,
                    size: 0,
                    sha256: None,
                });
                if let Some((dest, _)) = copy {
                    fs::create_dir_all(dest.join(relative))?;
                }
                visit(root, &path, copy, entries, total, files)?;
            } else if metadata.is_file() {
                if sensitive_file(&item.file_name()) {
                    continue;
                }
                *files += 1;
                *total = total
                    .checked_add(metadata.len())
                    .context("backup size overflow")?;
                if let Some((_, true)) = copy {
                    if *files > MAX_FILES {
                        bail!("backup exceeds {MAX_FILES} files")
                    }
                    if metadata.len() > MAX_FILE_BYTES {
                        bail!(
                            "backup file exceeds {MAX_FILE_BYTES} bytes: {}",
                            relative.display()
                        )
                    }
                    if *total > MAX_TOTAL_BYTES {
                        bail!("backup exceeds {MAX_TOTAL_BYTES} bytes")
                    }
                }
                let digest = sha(&path)?;
                if let Some((dest, _)) = copy {
                    let target = dest.join(relative);
                    if let Some(parent) = target.parent() {
                        fs::create_dir_all(parent)?
                    };
                    fs::copy(&path, target)?;
                }
                entries.push(Entry {
                    path: relative.into(),
                    kind: EntryKind::File,
                    size: metadata.len(),
                    sha256: Some(digest),
                });
            }
        }
        Ok(())
    }
    visit(root, root, copy, &mut entries, &mut total, &mut files)?;
    entries.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(entries)
}
fn validate_manifest(manifest: &Manifest) -> Result<()> {
    let mut seen = BTreeSet::new();
    let mut total = 0u64;
    let mut files = 0usize;
    for entry in &manifest.entries {
        safe_relative(&entry.path)?;
        if !seen.insert(&entry.path) {
            bail!("duplicate manifest path")
        };
        if entry.kind == EntryKind::File {
            files += 1;
            total += entry.size;
            if entry.size > MAX_FILE_BYTES {
                bail!("manifest file exceeds limit")
            }
        }
    }
    if files > MAX_FILES || total > MAX_TOTAL_BYTES {
        bail!("manifest exceeds backup limits")
    }
    Ok(())
}
fn ensure_managed_path(root: &Path, relative: &Path) -> Result<()> {
    safe_relative(relative)?;
    let mut current = root.to_path_buf();
    for component in relative.parent().into_iter().flat_map(Path::components) {
        let Component::Normal(name) = component else {
            bail!("unsafe managed path")
        };
        current.push(name);
        if current.exists() && is_link_or_reparse(&fs::symlink_metadata(&current)?) {
            bail!("managed path crosses a symlink, junction, or reparse point")
        }
    }
    Ok(())
}
fn apply_tree(root: &Path, source: &Path, desired: &[Entry], current: &[Entry]) -> Result<()> {
    ensure_canonical_root(root)?;
    ensure_tree_root(source)?;
    let desired_paths: BTreeSet<_> = desired.iter().map(|e| e.path.clone()).collect();
    for entry in current.iter().rev() {
        if !desired_paths.contains(&entry.path) {
            ensure_managed_path(root, &entry.path)?;
            let target = root.join(&entry.path);
            if entry.kind == EntryKind::File {
                fs::remove_file(target)?;
            } else if target.exists() {
                if let Err(error) = fs::remove_dir(target) {
                    if error.kind() != std::io::ErrorKind::DirectoryNotEmpty {
                        return Err(error.into());
                    }
                }
            }
        }
    }
    for entry in desired.iter().filter(|e| e.kind == EntryKind::Directory) {
        ensure_managed_path(root, &entry.path)?;
        fs::create_dir_all(root.join(&entry.path))?;
    }
    for entry in desired.iter().filter(|e| e.kind == EntryKind::File) {
        ensure_managed_path(root, &entry.path)?;
        ensure_managed_path(source, &entry.path)?;
        let from = source.join(&entry.path);
        if sha(&from)? != entry.sha256.clone().unwrap_or_default() {
            bail!("snapshot hash mismatch: {}", entry.path.display())
        }
        let target = root.join(&entry.path);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?
        };
        fs::copy(from, target)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (PathBuf, PathBuf, BackupStore) {
        let base = std::env::temp_dir().join(format!("agent-backup-test-{}", uuid::Uuid::new_v4()));
        let root = base.join("root");
        let store_dir = base.join("store");
        fs::create_dir_all(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let store = BackupStore::new(store_dir).unwrap();
        (base, root, store)
    }
    #[test]
    fn rejects_manifest_traversal_and_all_limits() {
        let (_, root, _) = fixture();
        let base = Manifest {
            task_id: "t".into(),
            root_id: "root".into(),
            root,
            limits: Limits {
                total_bytes: MAX_TOTAL_BYTES,
                files: MAX_FILES,
                single_file_bytes: MAX_FILE_BYTES,
            },
            retention_policy: "retained_until_explicit_cleanup".into(),
            excluded_directories: vec![],
            excluded_sensitive_files: vec![],
            entries: vec![],
        };
        let mut traversal = base.clone();
        traversal.entries.push(Entry {
            path: "../escape".into(),
            kind: EntryKind::File,
            size: 1,
            sha256: Some("x".into()),
        });
        assert!(validate_manifest(&traversal).is_err());
        let mut single = base.clone();
        single.entries.push(Entry {
            path: "big".into(),
            kind: EntryKind::File,
            size: MAX_FILE_BYTES + 1,
            sha256: Some("x".into()),
        });
        assert!(validate_manifest(&single).is_err());
        let mut count = base.clone();
        count.entries = (0..=MAX_FILES)
            .map(|i| Entry {
                path: PathBuf::from(format!("f{i}")),
                kind: EntryKind::File,
                size: 0,
                sha256: Some("x".into()),
            })
            .collect();
        assert!(validate_manifest(&count).is_err());
        let mut total = base;
        total.entries = (0..6)
            .map(|i| Entry {
                path: PathBuf::from(format!("f{i}")),
                kind: EntryKind::File,
                size: MAX_FILE_BYTES,
                sha256: Some("x".into()),
            })
            .collect();
        assert!(validate_manifest(&total).is_err());
    }
    #[test]
    fn restores_modified_deleted_and_new_files_but_preserves_exclusions() {
        let (base, root, store) = fixture();
        fs::write(root.join("modified.txt"), "before").unwrap();
        fs::write(root.join("deleted.txt"), "restore").unwrap();
        fs::write(root.join(".env"), "TOKEN=before").unwrap();
        fs::create_dir(root.join("node_modules")).unwrap();
        fs::write(root.join("node_modules/keep.txt"), "old generated").unwrap();
        store.create("task-1", "root-1", &root).unwrap();
        fs::write(root.join("modified.txt"), "after").unwrap();
        fs::remove_file(root.join("deleted.txt")).unwrap();
        fs::write(root.join("new.txt"), "remove").unwrap();
        fs::write(root.join("node_modules/keep.txt"), "new generated").unwrap();
        fs::write(root.join(".env"), "TOKEN=after").unwrap();
        store.rollback("task-1", "root-1", &root).unwrap();
        assert_eq!(
            fs::read_to_string(root.join("modified.txt")).unwrap(),
            "before"
        );
        assert_eq!(
            fs::read_to_string(root.join("deleted.txt")).unwrap(),
            "restore"
        );
        assert!(!root.join("new.txt").exists());
        assert_eq!(
            fs::read_to_string(root.join("node_modules/keep.txt")).unwrap(),
            "new generated"
        );
        assert_eq!(
            fs::read_to_string(root.join(".env")).unwrap(),
            "TOKEN=after"
        );
        assert!(store.snapshot_dir("task-1").exists());
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn rejects_wrong_task_and_root() {
        let (base, root, store) = fixture();
        fs::write(root.join("a"), "a").unwrap();
        store.create("task-1", "root-1", &root).unwrap();
        assert!(store.load("task-2").is_err());
        let other = base.join("other");
        fs::create_dir(&other).unwrap();
        let other = fs::canonicalize(other).unwrap();
        assert!(store.rollback("task-1", "root-1", &other).is_err());
        assert!(store.rollback("task-1", "wrong-root", &root).is_err());
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn oversized_snapshot_fails_without_publishing() {
        let (base, root, store) = fixture();
        let file = fs::File::create(root.join("oversized.bin")).unwrap();
        file.set_len(MAX_FILE_BYTES + 1).unwrap();
        assert!(store.create("large-task", "root", &root).is_err());
        assert!(!store.snapshot_dir("large-task").exists());
        fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn rejects_symlink_or_reparse_entries_when_supported() {
        let (base, root, store) = fixture();
        let outside = base.join("outside");
        fs::write(&outside, "secret").unwrap();
        #[cfg(windows)]
        let linked = std::os::windows::fs::symlink_file(&outside, root.join("link"));
        #[cfg(unix)]
        let linked = std::os::unix::fs::symlink(&outside, root.join("link"));
        if linked.is_ok() {
            assert!(store.create("task", "root", &root).is_err());
        }
        fs::remove_dir_all(base).unwrap();
    }
}
