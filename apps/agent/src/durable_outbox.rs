use anyhow::{Context, Result, bail};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

const MAX_EVENTS: usize = 2_048;
const MAX_EVENT_BYTES: usize = 256 * 1024;
const MAX_TOTAL_BYTES: usize = 16 * 1024 * 1024;

pub struct DurableOutbox {
    path: PathBuf,
    events: BTreeMap<String, Value>,
}

impl DurableOutbox {
    pub fn load(path: PathBuf) -> Result<Self> {
        validate_path(&path)?;
        let events = if path.exists() {
            if fs::metadata(&path)?.len() > (MAX_TOTAL_BYTES + MAX_EVENTS * 512) as u64 {
                bail!("durable event outbox file exceeds size limit")
            }
            serde_json::from_slice(&fs::read(&path).context("failed to read durable event outbox")?)
                .context("durable event outbox is corrupt")?
        } else {
            BTreeMap::new()
        };
        let result = Self { path, events };
        result.validate()?;
        Ok(result)
    }

    pub fn values(&self) -> impl Iterator<Item = &Value> {
        self.events.values()
    }

    pub fn insert(&mut self, event: Value) -> Result<()> {
        validate_event(&event)?;
        let key = event["source_event_id"].as_str().unwrap().to_owned();
        if let Some(existing) = self.events.get(&key) {
            if existing == &event {
                return Ok(());
            }
            bail!("durable outbox source_event_id collision")
        }
        let previous = self.events.insert(key.clone(), event);
        if let Err(error) = self.validate().and_then(|_| self.persist()) {
            match previous {
                Some(value) => {
                    self.events.insert(key, value);
                }
                None => {
                    self.events.remove(&key);
                }
            }
            return Err(error);
        }
        Ok(())
    }

    pub fn remove(&mut self, source_event_id: &str) -> Result<bool> {
        let Some(previous) = self.events.remove(source_event_id) else {
            return Ok(false);
        };
        if let Err(error) = self.persist() {
            self.events.insert(source_event_id.to_owned(), previous);
            return Err(error);
        }
        Ok(true)
    }

    pub fn has_terminal(&self, task_id: &str) -> bool {
        self.events.values().any(|event| {
            event["task_id"] == task_id
                && matches!(
                    event["event_type"].as_str(),
                    Some("turn/completed" | "turn/failed" | "turn/cancelled" | "turn/canceled")
                )
        })
    }

    pub fn ensure_recovery_failure(&mut self, task_id: &str, reason: &str) -> Result<bool> {
        if self.has_terminal(task_id) {
            return Ok(false);
        }
        let source = format!("agent-recovery:{task_id}");
        self.insert(serde_json::json!({"type":"task.event","task_id":task_id,"source_event_id":source,"event_type":"turn/failed","payload":{"error":reason}}))?;
        Ok(true)
    }

    fn validate(&self) -> Result<()> {
        if self.events.len() > MAX_EVENTS {
            bail!("durable outbox exceeds event count limit")
        }
        let mut total = 0usize;
        for (key, event) in &self.events {
            validate_event(event)?;
            if event["source_event_id"].as_str() != Some(key) {
                bail!("durable outbox key does not match source_event_id")
            }
            let size = serde_json::to_vec(event)?.len();
            if size > MAX_EVENT_BYTES {
                bail!("durable outbox event exceeds size limit")
            }
            total = total
                .checked_add(size)
                .context("durable outbox size overflow")?;
        }
        if total > MAX_TOTAL_BYTES {
            bail!("durable outbox exceeds total size limit")
        }
        Ok(())
    }

    fn persist(&self) -> Result<()> {
        validate_path(&self.path)?;
        let parent = self
            .path
            .parent()
            .context("durable outbox path has no parent")?;
        fs::create_dir_all(parent)?;
        validate_path(&self.path)?;
        let temporary = parent.join(format!(".outbox-{}.tmp", uuid::Uuid::new_v4()));
        fs::write(&temporary, serde_json::to_vec(&self.events)?)?;
        fs::OpenOptions::new()
            .write(true)
            .open(&temporary)?
            .sync_all()?;
        if let Err(error) = atomic_publish(&temporary, &self.path) {
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
        Ok(())
    }
}

#[cfg(windows)]
fn atomic_publish(temporary: &Path, target: &Path) -> Result<()> {
    if !target.exists() {
        fs::rename(temporary, target)?;
        return Ok(());
    }
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::ReplaceFileW;
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    let temporary: Vec<u16> = temporary.as_os_str().encode_wide().chain(Some(0)).collect();
    let replaced = unsafe {
        ReplaceFileW(
            target.as_ptr(),
            temporary.as_ptr(),
            std::ptr::null(),
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if replaced == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}

#[cfg(not(windows))]
fn atomic_publish(temporary: &Path, target: &Path) -> Result<()> {
    fs::rename(temporary, target)?;
    Ok(())
}

fn validate_event(event: &Value) -> Result<()> {
    if event["type"] != "task.event"
        || event["task_id"].as_str().is_none()
        || event["source_event_id"].as_str().is_none()
        || event["event_type"].as_str().is_none()
        || !event["payload"].is_object()
    {
        bail!("durable outbox contains an invalid task.event")
    }
    Ok(())
}

fn validate_path(path: &Path) -> Result<()> {
    if !path.is_absolute() || path.file_name().and_then(|v| v.to_str()) != Some("event-outbox.json")
    {
        bail!("durable outbox path is invalid")
    }
    for ancestor in path.ancestors().skip(1).filter(|p| p.exists()) {
        let metadata = fs::symlink_metadata(ancestor)?;
        if metadata.file_type().is_symlink() || is_reparse(&metadata) {
            bail!("durable outbox path contains a symlink or reparse point")
        }
    }
    if path.exists() {
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.is_file() || metadata.file_type().is_symlink() || is_reparse(&metadata) {
            bail!("durable outbox file is unsafe")
        }
    }
    Ok(())
}

#[cfg(windows)]
fn is_reparse(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    metadata.file_attributes() & 0x400 != 0
}
#[cfg(not(windows))]
fn is_reparse(_: &fs::Metadata) -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    fn event(id: &str, task: &str, kind: &str) -> Value {
        serde_json::json!({"type":"task.event","task_id":task,"source_event_id":id,"event_type":kind,"payload":{}})
    }
    #[test]
    fn persists_reloads_and_durably_removes() {
        let dir = std::env::temp_dir().join(format!("outbox-{}", uuid::Uuid::new_v4()));
        let path = dir.join("event-outbox.json");
        let mut outbox = DurableOutbox::load(path.clone()).unwrap();
        outbox.insert(event("a", "t", "text.delta")).unwrap();
        assert_eq!(
            DurableOutbox::load(path.clone()).unwrap().values().count(),
            1
        );
        outbox.remove("a").unwrap();
        assert_eq!(DurableOutbox::load(path).unwrap().values().count(), 0);
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn rejects_corrupt_and_oversized() {
        let dir = std::env::temp_dir().join(format!("outbox-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("event-outbox.json");
        fs::write(&path, b"{").unwrap();
        assert!(DurableOutbox::load(path.clone()).is_err());
        fs::write(
            &path,
            serde_json::to_vec(&BTreeMap::from([("x", event("x", "t", "x"))])).unwrap(),
        )
        .unwrap();
        let mut value = event("big", "t", "x");
        value["payload"] = serde_json::json!({"x":"x".repeat(MAX_EVENT_BYTES)});
        let mut outbox = DurableOutbox::load(path).unwrap();
        assert!(outbox.insert(value).is_err());
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn recovery_is_idempotent_and_completed_wins() {
        let dir = std::env::temp_dir().join(format!("outbox-{}", uuid::Uuid::new_v4()));
        let path = dir.join("event-outbox.json");
        let mut outbox = DurableOutbox::load(path).unwrap();
        assert!(outbox.ensure_recovery_failure("t", "crash").unwrap());
        let path = outbox.path.clone();
        drop(outbox);
        let mut outbox = DurableOutbox::load(path).unwrap();
        assert!(!outbox.ensure_recovery_failure("t", "again").unwrap());
        outbox.insert(event("done", "u", "turn/completed")).unwrap();
        assert!(!outbox.ensure_recovery_failure("u", "crash").unwrap());
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn recognizes_all_terminal_turn_spellings() {
        for (index, kind) in [
            "turn/completed",
            "turn/failed",
            "turn/cancelled",
            "turn/canceled",
        ]
        .into_iter()
        .enumerate()
        {
            let dir = std::env::temp_dir().join(format!("outbox-{}", uuid::Uuid::new_v4()));
            let mut outbox = DurableOutbox::load(dir.join("event-outbox.json")).unwrap();
            outbox
                .insert(event(&format!("terminal-{index}"), "task", kind))
                .unwrap();
            assert!(outbox.has_terminal("task"), "terminal kind missed: {kind}");
            assert!(!outbox.ensure_recovery_failure("task", "crash").unwrap());
            let _ = fs::remove_dir_all(dir);
        }
    }
    #[test]
    fn unacknowledged_send_survives_restart() {
        let dir = std::env::temp_dir().join(format!("outbox-{}", uuid::Uuid::new_v4()));
        let path = dir.join("event-outbox.json");
        let mut outbox = DurableOutbox::load(path.clone()).unwrap();
        outbox
            .insert(event("not-acked", "t", "text.delta"))
            .unwrap();
        drop(outbox);
        assert_eq!(
            DurableOutbox::load(path).unwrap().values().next().unwrap()["source_event_id"],
            "not-acked"
        );
        let _ = fs::remove_dir_all(dir);
    }
    #[test]
    fn rejects_symlink_outbox_when_platform_allows_creation() {
        let dir = std::env::temp_dir().join(format!("outbox-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let target = dir.join("target.json");
        fs::write(&target, b"{}").unwrap();
        let link = dir.join("event-outbox.json");
        #[cfg(windows)]
        let linked = std::os::windows::fs::symlink_file(&target, &link).is_ok();
        #[cfg(unix)]
        let linked = std::os::unix::fs::symlink(&target, &link).is_ok();
        if linked {
            assert!(DurableOutbox::load(link).is_err());
        }
        let _ = fs::remove_dir_all(dir);
    }
}
