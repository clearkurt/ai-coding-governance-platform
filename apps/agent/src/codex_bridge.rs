//! Fixed-runtime Codex App Server bridge. It is isolated from the legacy
//! sandbox path until the server-side migration feature switch is enabled.

use anyhow::{Context, Result, anyhow, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, VecDeque},
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{Mutex, mpsc, oneshot},
};

#[cfg(windows)]
struct KillOnCloseJob(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
impl KillOnCloseJob {
    fn assign(child: &Child) -> Result<Self> {
        use windows_sys::Win32::{
            Foundation::{CloseHandle, HANDLE},
            System::JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
                SetInformationJobObject,
            },
        };
        let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if job.is_null() {
            bail!("failed to create Codex Job Object")
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of_val(&info) as u32,
            )
        };
        let process = child
            .raw_handle()
            .context("Codex process handle unavailable")? as HANDLE;
        let assigned = unsafe { AssignProcessToJobObject(job, process) };
        if configured == 0 || assigned == 0 {
            unsafe { CloseHandle(job) };
            bail!("failed to assign Codex process to Job Object")
        }
        Ok(Self(job))
    }
}
#[cfg(windows)]
impl Drop for KillOnCloseJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

pub const PINNED_PROTOCOL_VERSION: &str = "0.145.0-alpha.27";
pub const PINNED_APP_SERVER_SCHEMA_VERSION: &str = "app-server-schema-1";
pub const PINNED_MODEL_CATALOG_VERSION: &str = "deepseek-v4-flash-1";
pub const PINNED_CONFIG_TEMPLATE_VERSION: &str = "company-responses-1";
const STDERR_LIMIT: usize = 64 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReleaseManifest {
    pub version: String,
    pub sha256: String,
    pub schema_version: String,
    pub model_catalog_version: String,
    pub config_template_version: String,
}

#[derive(Clone, Debug)]
pub struct RuntimeConfig {
    pub managed_runtime_dir: PathBuf,
    pub release: ReleaseManifest,
    pub request_timeout: Duration,
    pub codex_home: PathBuf,
    pub responses_base_url: String,
    pub auth_command: PathBuf,
}

#[derive(Serialize)]
struct ManagedCodexConfig {
    model: String,
    model_provider: String,
    approval_policy: String,
    sandbox_mode: String,
    sandbox_workspace_write: ManagedWorkspaceWrite,
    model_providers: BTreeMap<String, ManagedProvider>,
}
#[derive(Serialize)]
struct ManagedWorkspaceWrite {
    network_access: bool,
}
#[derive(Serialize)]
struct ManagedProvider {
    name: String,
    base_url: String,
    wire_api: String,
    auth: ManagedAuth,
}
#[derive(Serialize)]
struct ManagedAuth {
    command: String,
    args: Vec<String>,
    refresh_interval_ms: u64,
    timeout_ms: u64,
}

impl RuntimeConfig {
    pub fn validate(&self) -> Result<PathBuf> {
        validate_release_manifest(&self.release)?;
        if !self.managed_runtime_dir.is_absolute() {
            bail!("managed runtime directory must be absolute")
        }
        let release_dir = self
            .managed_runtime_dir
            .join("releases")
            .join(&self.release.version);
        let installed: ReleaseManifest = serde_json::from_slice(
            &std::fs::read(release_dir.join("release.json"))
                .context("installed Codex release manifest is missing")?,
        )?;
        if installed != self.release {
            bail!("installed Codex release manifest does not match configured pin")
        }
        let executable = release_dir
            .join(if cfg!(windows) { "codex.exe" } else { "codex" })
            .canonicalize()
            .context("pinned Codex runtime is missing")?;
        if !executable.is_file() {
            bail!("pinned Codex runtime is not a file")
        }
        if !executable.starts_with(self.managed_runtime_dir.canonicalize()?) {
            bail!("Codex runtime escaped the company-managed directory")
        }
        let actual = hex_sha256(&std::fs::read(&executable)?);
        if !actual.eq_ignore_ascii_case(&self.release.sha256) {
            bail!("pinned Codex runtime SHA-256 mismatch")
        }
        Ok(executable)
    }

    fn write_managed_config(&self) -> Result<()> {
        if !self.codex_home.is_absolute() || !self.auth_command.is_absolute() {
            bail!("managed Codex paths must be absolute")
        }
        if !(self.responses_base_url.starts_with("https://")
            || self.responses_base_url.starts_with("http://localhost")
            || self.responses_base_url.starts_with("http://127.0.0.1"))
        {
            bail!("Responses proxy must use HTTPS or localhost HTTP")
        }
        std::fs::create_dir_all(&self.codex_home)?;
        let provider = ManagedProvider {
            name: "Company DeepSeek".into(),
            base_url: self.responses_base_url.trim_end_matches('/').into(),
            wire_api: "responses".into(),
            auth: ManagedAuth {
                command: self.auth_command.to_string_lossy().into_owned(),
                args: vec!["model-token".into()],
                refresh_interval_ms: 0,
                timeout_ms: 5_000,
            },
        };
        let config = ManagedCodexConfig {
            model: "deepseek-v4-flash".into(),
            model_provider: "company".into(),
            approval_policy: "on-request".into(),
            sandbox_mode: "workspace-write".into(),
            sandbox_workspace_write: ManagedWorkspaceWrite {
                network_access: false,
            },
            model_providers: BTreeMap::from([("company".into(), provider)]),
        };
        let bytes = toml::to_string_pretty(&config)?.into_bytes();
        let target = self.codex_home.join("config.toml");
        let temporary = self.codex_home.join("config.toml.tmp");
        std::fs::write(&temporary, bytes)?;
        if target.exists() {
            std::fs::remove_file(&target)?;
        }
        std::fs::rename(temporary, target)?;
        Ok(())
    }
}

pub fn validate_release_manifest(release: &ReleaseManifest) -> Result<()> {
    for (name, actual, expected) in [
        ("version", release.version.as_str(), PINNED_PROTOCOL_VERSION),
        (
            "schema_version",
            release.schema_version.as_str(),
            PINNED_APP_SERVER_SCHEMA_VERSION,
        ),
        (
            "model_catalog_version",
            release.model_catalog_version.as_str(),
            PINNED_MODEL_CATALOG_VERSION,
        ),
        (
            "config_template_version",
            release.config_template_version.as_str(),
            PINNED_CONFIG_TEMPLATE_VERSION,
        ),
    ] {
        if actual != expected {
            bail!("release {name} must equal this daemon's supported value {expected}")
        }
    }
    if release.sha256.len() != 64 || !release.sha256.chars().all(|c| c.is_ascii_hexdigit()) {
        bail!("release SHA-256 must contain exactly 64 hexadecimal characters")
    }
    Ok(())
}

fn codex_version_matches(stdout: &[u8], expected: &str) -> bool {
    let Ok(output) = std::str::from_utf8(stdout) else {
        return false;
    };
    let mut tokens = output.split_whitespace();
    matches!(
        (tokens.next(), tokens.next(), tokens.next()),
        (Some("codex-cli"), Some(version), None) if version == expected
    )
}

pub fn install_release(
    managed_runtime_dir: &Path,
    artifact: &Path,
    release: &ReleaseManifest,
) -> Result<PathBuf> {
    validate_release_manifest(release)?;
    if !managed_runtime_dir.is_absolute() {
        bail!("managed runtime directory must be absolute")
    }
    if !artifact.is_file() {
        bail!("Codex release artifact is missing")
    }
    let actual = hex_sha256(&std::fs::read(artifact)?);
    if !actual.eq_ignore_ascii_case(&release.sha256) {
        bail!("Codex release artifact SHA-256 mismatch")
    }
    let output = std::process::Command::new(artifact)
        .arg("--version")
        .output()
        .context("failed to verify Codex release version")?;
    if !output.status.success() || !codex_version_matches(&output.stdout, &release.version) {
        bail!("Codex release version mismatch")
    }
    std::fs::create_dir_all(managed_runtime_dir.join("releases"))?;
    let final_dir = managed_runtime_dir.join("releases").join(&release.version);
    let temporary = managed_runtime_dir
        .join("releases")
        .join(format!(".install-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&temporary)?;
    let executable = temporary.join(if cfg!(windows) { "codex.exe" } else { "codex" });
    let result = (|| {
        std::fs::copy(artifact, &executable)?;
        if hex_sha256(&std::fs::read(&executable)?) != actual {
            bail!("installed Codex artifact verification failed")
        };
        std::fs::write(
            temporary.join("release.json"),
            serde_json::to_vec_pretty(release)?,
        )?;
        let previous = managed_runtime_dir
            .join("releases")
            .join(format!(".previous-{}", uuid::Uuid::new_v4()));
        if final_dir.exists() {
            std::fs::rename(&final_dir, &previous)?;
        }
        if let Err(error) = std::fs::rename(&temporary, &final_dir) {
            if previous.exists() {
                let _ = std::fs::rename(&previous, &final_dir);
            }
            return Err(error.into());
        }
        if previous.exists() {
            let _ = std::fs::remove_dir_all(previous);
        }
        Ok(final_dir.join(if cfg!(windows) { "codex.exe" } else { "codex" }))
    })();
    if result.is_err() {
        let _ = std::fs::remove_dir_all(temporary);
    }
    result
}

fn hex_sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[derive(Clone, Debug)]
pub struct AppNotification {
    pub method: String,
    pub params: Value,
    pub source_event_id: String,
}

impl AppNotification {
    fn from_value(value: Value, ordinal: u64) -> Option<Self> {
        let method = value.get("method")?.as_str()?.to_owned();
        let params = value.get("params").cloned().unwrap_or(Value::Null);
        let payload = serde_json::to_vec(&params).ok()?;
        let digest = hex_sha256(&payload);
        Some(Self {
            source_event_id: format!("codex:{ordinal}:{}", &digest[..16]),
            method,
            params,
        })
    }

    pub fn is_approval(&self) -> bool {
        self.method.ends_with("/requestApproval")
    }
}

#[derive(Clone, Debug)]
pub struct ServerRequest {
    pub id: u64,
    pub method: String,
    pub params: Value,
}

pub struct CodexAppServer {
    child: Mutex<Child>,
    stdin: Mutex<ChildStdin>,
    pending: Arc<Mutex<BTreeMap<u64, oneshot::Sender<Result<Value>>>>>,
    next_id: Mutex<u64>,
    pub notifications: mpsc::Receiver<AppNotification>,
    pub server_requests: mpsc::Receiver<ServerRequest>,
    stderr: Arc<Mutex<VecDeque<u8>>>,
    timeout: Duration,
    #[cfg(windows)]
    _job: KillOnCloseJob,
}

impl CodexAppServer {
    pub async fn start(config: RuntimeConfig) -> Result<Self> {
        let executable = config.validate()?;
        config.write_managed_config()?;
        let output = Command::new(&executable)
            .arg("--version")
            .output()
            .await
            .context("failed to check pinned Codex version")?;
        if !output.status.success()
            || !codex_version_matches(&output.stdout, &config.release.version)
        {
            bail!("pinned Codex version does not match configured version")
        }
        let mut child = Command::new(executable)
            .args(["app-server", "--stdio", "--strict-config"])
            .env("CODEX_HOME", &config.codex_home)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .context("failed to start pinned Codex App Server")?;
        let stdin = child
            .stdin
            .take()
            .context("Codex App Server stdin unavailable")?;
        let stdout = child
            .stdout
            .take()
            .context("Codex App Server stdout unavailable")?;
        let stderr_reader = child
            .stderr
            .take()
            .context("Codex App Server stderr unavailable")?;
        #[cfg(windows)]
        let job = KillOnCloseJob::assign(&child)?;
        let pending: Arc<Mutex<BTreeMap<u64, oneshot::Sender<Result<Value>>>>> =
            Arc::new(Mutex::new(BTreeMap::new()));
        let (notify_tx, notify_rx) = mpsc::channel(256);
        let (request_tx, request_rx) = mpsc::channel(64);
        let stderr = Arc::new(Mutex::new(VecDeque::new()));
        Self::spawn_stdout_reader(stdout, pending.clone(), notify_tx, request_tx);
        Self::spawn_stderr_reader(stderr_reader, stderr.clone());
        let server = Self {
            child: Mutex::new(child),
            stdin: Mutex::new(stdin),
            pending,
            next_id: Mutex::new(1),
            notifications: notify_rx,
            server_requests: request_rx,
            stderr,
            timeout: config.request_timeout,
            #[cfg(windows)]
            _job: job,
        };
        server
            .request(
                "initialize",
                json!({"clientInfo":{"name":"company-agent","version":env!("CARGO_PKG_VERSION")}}),
            )
            .await?;
        server.notify("initialized", Value::Null).await?;
        Ok(server)
    }

    fn spawn_stdout_reader(
        stdout: tokio::process::ChildStdout,
        pending: Arc<Mutex<BTreeMap<u64, oneshot::Sender<Result<Value>>>>>,
        notify: mpsc::Sender<AppNotification>,
        requests: mpsc::Sender<ServerRequest>,
    ) {
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            let ordinal = AtomicU64::new(0);
            let event_stream_id = uuid::Uuid::new_v4().to_string();
            while let Ok(Some(line)) = lines.next_line().await {
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if let (Some(id), Some(method)) = (
                    value.get("id").and_then(Value::as_u64),
                    value.get("method").and_then(Value::as_str),
                ) {
                    let _ = requests
                        .send(ServerRequest {
                            id,
                            method: method.to_owned(),
                            params: value.get("params").cloned().unwrap_or(Value::Null),
                        })
                        .await;
                } else if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    if let Some(sender) = pending.lock().await.remove(&id) {
                        let result = if let Some(error) = value.get("error") {
                            Err(anyhow!("Codex JSON-RPC error: {error}"))
                        } else {
                            Ok(value.get("result").cloned().unwrap_or(Value::Null))
                        };
                        let _ = sender.send(result);
                    }
                } else if let Some(mut notification) =
                    AppNotification::from_value(value, ordinal.fetch_add(1, Ordering::Relaxed) + 1)
                {
                    notification.source_event_id =
                        format!("{event_stream_id}:{}", notification.source_event_id);
                    let _ = notify.send(notification).await;
                }
            }
            for (_, sender) in pending.lock().await.split_off(&0) {
                let _ = sender.send(Err(anyhow!("Codex App Server stdout closed")));
            }
        });
    }

    fn spawn_stderr_reader(
        stderr_reader: tokio::process::ChildStderr,
        stderr: Arc<Mutex<VecDeque<u8>>>,
    ) {
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr_reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let mut log = stderr.lock().await;
                log.extend(line.bytes());
                log.push_back(b'\n');
                while log.len() > STDERR_LIMIT {
                    log.pop_front();
                }
            }
        });
    }

    pub async fn request(&self, method: &str, params: Value) -> Result<Value> {
        if self.child.lock().await.try_wait()?.is_some() {
            bail!("Codex App Server exited: {}", self.stderr_text().await)
        }
        let mut next = self.next_id.lock().await;
        let id = *next;
        *next += 1;
        drop(next);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let line = serde_json::to_string(
            &json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}),
        )? + "\n";
        if let Err(error) = self.stdin.lock().await.write_all(line.as_bytes()).await {
            self.pending.lock().await.remove(&id);
            return Err(error.into());
        }
        match tokio::time::timeout(self.timeout, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(anyhow!("Codex request receiver closed")),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                bail!("Codex {method} timed out")
            }
        }
    }
    pub async fn notify(&self, method: &str, params: Value) -> Result<()> {
        let mut value = json!({"jsonrpc":"2.0","method":method});
        if !params.is_null() {
            value["params"] = params;
        }
        let line = serde_json::to_string(&value)? + "\n";
        self.stdin.lock().await.write_all(line.as_bytes()).await?;
        Ok(())
    }
    pub async fn respond(&self, id: u64, result: Result<Value>) -> Result<()> {
        let value = match result {
            Ok(result) => json!({"jsonrpc":"2.0","id":id,"result":result}),
            Err(error) => {
                json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":error.to_string()}})
            }
        };
        let line = serde_json::to_string(&value)? + "\n";
        self.stdin.lock().await.write_all(line.as_bytes()).await?;
        Ok(())
    }

    pub async fn start_thread(&self, cwd: &Path) -> Result<String> {
        let result = self
            .request(
                "thread/start",
                json!({"cwd":cwd,"approvalPolicy":"on-request","sandbox":"workspace-write"}),
            )
            .await?;
        result
            .pointer("/thread/id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .context("thread/start did not return thread.id")
    }
    pub async fn start_turn(&self, thread_id: &str, cwd: &Path, prompt: &str) -> Result<String> {
        let result = self
            .request(
                "turn/start",
                json!({"threadId":thread_id,"cwd":cwd,"input":[{"type":"text","text":prompt}]}),
            )
            .await?;
        result
            .pointer("/turn/id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .context("turn/start did not return turn.id")
    }
    pub async fn interrupt(&self, thread_id: &str, turn_id: &str) -> Result<()> {
        self.request(
            "turn/interrupt",
            json!({"threadId":thread_id,"turnId":turn_id}),
        )
        .await
        .map(|_| ())
    }
    pub async fn stderr_text(&self) -> String {
        String::from_utf8_lossy(&self.stderr.lock().await.iter().copied().collect::<Vec<_>>())
            .into_owned()
    }
    pub async fn is_running(&self) -> Result<bool> {
        Ok(self.child.lock().await.try_wait()?.is_none())
    }
    pub async fn shutdown(&self) -> Result<()> {
        let mut child = self.child.lock().await;
        if child.try_wait()?.is_none() {
            child.kill().await?;
        }
        Ok(())
    }
}

#[derive(Default)]
pub struct DispatchBook {
    active: BTreeMap<String, (String, String)>,
}
impl DispatchBook {
    pub fn register(&mut self, task_id: String, thread_id: String, turn_id: String) -> bool {
        if self.active.contains_key(&task_id) {
            return false;
        }
        self.active.insert(task_id, (thread_id, turn_id));
        true
    }
    pub fn interrupt_params(&self, task_id: &str) -> Option<(&str, &str)> {
        self.active
            .get(task_id)
            .map(|(a, b)| (a.as_str(), b.as_str()))
    }

    pub fn task_for_event(&self, thread_id: &str, turn_id: &str) -> Option<&str> {
        self.active.iter().find_map(|(task_id, (thread, turn))| {
            (thread == thread_id && (turn_id.is_empty() || turn == turn_id))
                .then_some(task_id.as_str())
        })
    }
    pub fn remove(&mut self, task_id: &str) -> bool {
        self.active.remove(task_id).is_some()
    }
    pub fn task_ids(&self) -> impl Iterator<Item = &str> {
        self.active.keys().map(String::as_str)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, process::Command as StdCommand};

    fn compile_mock_server() -> PathBuf {
        compile_mock_server_with_version(PINNED_PROTOCOL_VERSION)
    }

    fn compile_mock_server_with_version(version: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("codex-mock-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let source = dir.join("mock.rs");
        let executable = dir.join(if cfg!(windows) {
            "mock-codex.exe"
        } else {
            "mock-codex"
        });
        let source_text = r###"
use std::io::{self, BufRead};
fn id(line: &str) -> u64 {
    let marker = "\"id\":";
    let start = line.find(marker).unwrap() + marker.len();
    line[start..].chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse().unwrap()
}
fn main() {
    if std::env::args().any(|arg| arg == "--version") { println!("codex-cli __MOCK_VERSION__"); return; }
    let mut slow = None;
    for line in io::stdin().lock().lines() {
        let line = line.unwrap();
        if line.contains("\"method\":\"initialized\"") { continue; }
        if line.contains("\"id\":900") { println!(r#"{{"jsonrpc":"2.0","method":"mock/responded","params":{{"threadId":"thread-1","turnId":"turn-1"}}}}"#); continue; }
        let request_id = id(&line);
        if line.contains("\"method\":\"initialize\"") { println!(r#"{{"jsonrpc":"2.0","id":{},"result":{{}}}}"#, request_id); }
        else if line.contains("\"method\":\"thread/start\"") { println!(r#"{{"jsonrpc":"2.0","id":{},"result":{{"thread":{{"id":"thread-1"}}}}}}"#, request_id); }
        else if line.contains("\"method\":\"turn/start\"") {
            println!(r#"{{"jsonrpc":"2.0","id":{},"result":{{"turn":{{"id":"turn-1"}}}}}}"#, request_id);
            println!(r#"{{"jsonrpc":"2.0","method":"item/agentMessage/delta","params":{{"threadId":"thread-1","turnId":"turn-1","itemId":"item-1","delta":"x"}}}}"#);
            println!(r#"{{"jsonrpc":"2.0","id":900,"method":"item/commandExecution/requestApproval","params":{{"threadId":"thread-1","turnId":"turn-1","itemId":"item-2"}}}}"#);
        }
        else if line.contains("\"method\":\"slow\"") { slow = Some(request_id); }
        else if line.contains("\"method\":\"fast\"") { println!(r#"{{"jsonrpc":"2.0","id":{},"result":"fast"}}"#, request_id); if let Some(slow_id) = slow.take() { println!(r#"{{"jsonrpc":"2.0","id":{},"result":"slow"}}"#, slow_id); } }
        else if line.contains("\"method\":\"crash\"") { std::process::exit(23); }
        else if !line.contains("\"method\":\"timeout\"") { println!(r#"{{"jsonrpc":"2.0","id":{},"result":{{}}}}"#, request_id); }
    }
}
"###
        .replace("__MOCK_VERSION__", version);
        fs::write(&source, source_text).unwrap();
        let status = StdCommand::new("rustc")
            .arg(&source)
            .arg("-o")
            .arg(&executable)
            .status()
            .unwrap();
        assert!(status.success());
        executable
    }

    fn mock_config(executable: PathBuf, timeout: Duration) -> RuntimeConfig {
        let codex_home = std::env::temp_dir().join(format!("codex-home-{}", uuid::Uuid::new_v4()));
        let managed_runtime_dir =
            std::env::temp_dir().join(format!("codex-runtime-{}", uuid::Uuid::new_v4()));
        let release = ReleaseManifest {
            version: PINNED_PROTOCOL_VERSION.into(),
            sha256: hex_sha256(&fs::read(&executable).unwrap()),
            schema_version: PINNED_APP_SERVER_SCHEMA_VERSION.into(),
            model_catalog_version: PINNED_MODEL_CATALOG_VERSION.into(),
            config_template_version: PINNED_CONFIG_TEMPLATE_VERSION.into(),
        };
        install_release(&managed_runtime_dir, &executable, &release).unwrap();
        RuntimeConfig {
            managed_runtime_dir,
            release,
            request_timeout: timeout,
            codex_home,
            responses_base_url: "http://localhost:8081/v1".into(),
            auth_command: std::env::current_exe().unwrap(),
        }
    }
    #[test]
    fn rejects_relative_and_store_runtime_paths() {
        let config = RuntimeConfig {
            managed_runtime_dir: PathBuf::from("runtime"),
            release: ReleaseManifest {
                version: PINNED_PROTOCOL_VERSION.into(),
                sha256: "0".repeat(64),
                schema_version: PINNED_APP_SERVER_SCHEMA_VERSION.into(),
                model_catalog_version: PINNED_MODEL_CATALOG_VERSION.into(),
                config_template_version: PINNED_CONFIG_TEMPLATE_VERSION.into(),
            },
            request_timeout: Duration::from_secs(1),
            codex_home: std::env::temp_dir().join("codex-test-home"),
            responses_base_url: "http://localhost:8081/v1".into(),
            auth_command: std::env::current_exe().unwrap(),
        };
        assert!(config.validate().is_err());
    }
    #[test]
    fn release_manifest_requires_exact_build_contract() {
        let valid = ReleaseManifest {
            version: PINNED_PROTOCOL_VERSION.into(),
            sha256: "a".repeat(64),
            schema_version: PINNED_APP_SERVER_SCHEMA_VERSION.into(),
            model_catalog_version: PINNED_MODEL_CATALOG_VERSION.into(),
            config_template_version: PINNED_CONFIG_TEMPLATE_VERSION.into(),
        };
        assert!(validate_release_manifest(&valid).is_ok());
        let mut missing_hash = valid.clone();
        missing_hash.sha256.clear();
        assert!(validate_release_manifest(&missing_hash).is_err());
        let mut bad_hash = valid.clone();
        bad_hash.sha256 = "z".repeat(64);
        assert!(validate_release_manifest(&bad_hash).is_err());
        for mutate in [
            |release: &mut ReleaseManifest| release.version = "0.145.0".into(),
            |release: &mut ReleaseManifest| release.schema_version = "arbitrary".into(),
            |release: &mut ReleaseManifest| release.model_catalog_version = "arbitrary".into(),
            |release: &mut ReleaseManifest| release.config_template_version = "arbitrary".into(),
        ] {
            let mut mismatched = valid.clone();
            mutate(&mut mismatched);
            assert!(validate_release_manifest(&mismatched).is_err());
        }
    }
    #[test]
    fn version_probe_rejects_substrings_and_extra_tokens() {
        assert!(codex_version_matches(
            b"codex-cli 0.145.0-alpha.27\n",
            PINNED_PROTOCOL_VERSION
        ));
        assert!(!codex_version_matches(
            b"codex-cli 0.145.0-alpha.270\n",
            PINNED_PROTOCOL_VERSION
        ));
        assert!(!codex_version_matches(
            b"wrapper codex-cli 0.145.0-alpha.27\n",
            PINNED_PROTOCOL_VERSION
        ));
    }
    #[tokio::test]
    #[ignore = "requires COMPANY_AGENT_REAL_CODEX pointing to an approved pinned Codex artifact"]
    async fn real_pinned_codex_artifact_initializes_with_strict_config() {
        let Some(artifact) = std::env::var_os("COMPANY_AGENT_REAL_CODEX").map(PathBuf::from) else {
            eprintln!("skipped: COMPANY_AGENT_REAL_CODEX is not set");
            return;
        };
        let base = std::env::temp_dir().join(format!(
            "company-agent-real-codex-smoke-{}",
            uuid::Uuid::new_v4()
        ));
        let managed_runtime_dir = base.join("runtime");
        let release = ReleaseManifest {
            version: PINNED_PROTOCOL_VERSION.into(),
            sha256: hex_sha256(&fs::read(&artifact).expect("read real Codex artifact")),
            schema_version: PINNED_APP_SERVER_SCHEMA_VERSION.into(),
            model_catalog_version: PINNED_MODEL_CATALOG_VERSION.into(),
            config_template_version: PINNED_CONFIG_TEMPLATE_VERSION.into(),
        };
        let result = async {
            install_release(&managed_runtime_dir, &artifact, &release)?;
            let server = CodexAppServer::start(RuntimeConfig {
                managed_runtime_dir,
                release,
                request_timeout: Duration::from_secs(15),
                codex_home: base.join("codex-home"),
                responses_base_url: "http://127.0.0.1:9/v1".into(),
                auth_command: std::env::current_exe()?,
            })
            .await?;
            server.shutdown().await
        }
        .await;
        let _ = fs::remove_dir_all(&base);
        result.expect("real pinned Codex artifact must initialize and shut down");
    }
    #[test]
    fn install_is_managed_and_runtime_detects_tampering() {
        let artifact = compile_mock_server();
        let managed =
            std::env::temp_dir().join(format!("managed-runtime-{}", uuid::Uuid::new_v4()));
        let release = ReleaseManifest {
            version: PINNED_PROTOCOL_VERSION.into(),
            sha256: hex_sha256(&fs::read(&artifact).unwrap()),
            schema_version: PINNED_APP_SERVER_SCHEMA_VERSION.into(),
            model_catalog_version: PINNED_MODEL_CATALOG_VERSION.into(),
            config_template_version: PINNED_CONFIG_TEMPLATE_VERSION.into(),
        };
        let installed = install_release(&managed, &artifact, &release).unwrap();
        assert!(installed.starts_with(&managed));
        assert_ne!(installed, artifact);
        let config = RuntimeConfig {
            managed_runtime_dir: managed.clone(),
            release: release.clone(),
            request_timeout: Duration::from_secs(1),
            codex_home: managed.join("home"),
            responses_base_url: "http://localhost:8081/v1".into(),
            auth_command: std::env::current_exe().unwrap(),
        };
        assert_eq!(
            config.validate().unwrap(),
            fs::canonicalize(&installed).unwrap()
        );
        fs::write(&installed, b"tampered").unwrap();
        assert!(
            config
                .validate()
                .unwrap_err()
                .to_string()
                .contains("SHA-256")
        );
        fs::remove_dir_all(managed).unwrap();
    }
    #[test]
    fn install_rejects_hash_and_version_mismatch() {
        let artifact = compile_mock_server();
        let managed =
            std::env::temp_dir().join(format!("managed-runtime-{}", uuid::Uuid::new_v4()));
        let mut release = ReleaseManifest {
            version: PINNED_PROTOCOL_VERSION.into(),
            sha256: "0".repeat(64),
            schema_version: PINNED_APP_SERVER_SCHEMA_VERSION.into(),
            model_catalog_version: PINNED_MODEL_CATALOG_VERSION.into(),
            config_template_version: PINNED_CONFIG_TEMPLATE_VERSION.into(),
        };
        assert!(
            install_release(&managed, &artifact, &release)
                .unwrap_err()
                .to_string()
                .contains("SHA-256")
        );
        release.sha256 = hex_sha256(&fs::read(&artifact).unwrap());
        let wrong_path = compile_mock_server_with_version("0.145.0-alpha.270");
        release.sha256 = hex_sha256(&fs::read(&wrong_path).unwrap());
        assert!(
            install_release(&managed, &wrong_path, &release)
                .unwrap_err()
                .to_string()
                .contains("version mismatch")
        );
        let _ = fs::remove_file(wrong_path);
        let _ = fs::remove_dir_all(managed);
    }
    #[test]
    fn notification_event_ids_are_stable_and_approval_is_detected() {
        let value = json!({"method":"item/commandExecution/requestApproval","params":{"threadId":"t","turnId":"u","item":{"id":"i"}}});
        let first = AppNotification::from_value(value.clone(), 1).unwrap();
        let second = AppNotification::from_value(value, 2).unwrap();
        assert_ne!(first.source_event_id, second.source_event_id);
        assert!(first.is_approval());
    }
    #[test]
    fn managed_config_uses_command_auth_without_embedding_a_token() {
        let dir =
            std::env::temp_dir().join(format!("codex-managed-config-{}", uuid::Uuid::new_v4()));
        let config = RuntimeConfig {
            managed_runtime_dir: std::env::temp_dir().join("unused-runtime"),
            release: ReleaseManifest {
                version: PINNED_PROTOCOL_VERSION.into(),
                sha256: "0".repeat(64),
                schema_version: PINNED_APP_SERVER_SCHEMA_VERSION.into(),
                model_catalog_version: PINNED_MODEL_CATALOG_VERSION.into(),
                config_template_version: PINNED_CONFIG_TEMPLATE_VERSION.into(),
            },
            request_timeout: Duration::from_secs(1),
            codex_home: dir.clone(),
            responses_base_url: "https://agent.example/v1".into(),
            auth_command: std::env::current_exe().unwrap(),
        };
        config.write_managed_config().unwrap();
        let rendered = fs::read_to_string(dir.join("config.toml")).unwrap();
        assert!(rendered.contains("model = \"deepseek-v4-flash\""));
        assert!(rendered.contains("model_provider = \"company\""));
        assert!(rendered.contains("wire_api = \"responses\""));
        assert!(rendered.contains("args = [\"model-token\"]"));
        assert!(rendered.contains("refresh_interval_ms = 0"));
        assert!(rendered.contains("approval_policy = \"on-request\""));
        assert!(rendered.contains("sandbox_mode = \"workspace-write\""));
        assert!(rendered.contains("[sandbox_workspace_write]"));
        assert!(rendered.contains("network_access = false"));
        assert!(!rendered.contains("access_token"));
        fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn duplicate_dispatch_does_not_create_a_second_turn() {
        let mut book = DispatchBook::default();
        assert!(book.register("task".into(), "thread".into(), "turn".into()));
        assert!(!book.register("task".into(), "other".into(), "other".into()));
        assert_eq!(book.interrupt_params("task"), Some(("thread", "turn")));
    }

    #[tokio::test]
    async fn mock_process_handles_protocol_routing_and_out_of_order_responses() {
        let executable = compile_mock_server();
        let mut server = CodexAppServer::start(mock_config(executable, Duration::from_secs(2)))
            .await
            .unwrap();
        let cwd = std::env::temp_dir();
        assert_eq!(server.start_thread(&cwd).await.unwrap(), "thread-1");
        assert_eq!(
            server.start_turn("thread-1", &cwd, "hello").await.unwrap(),
            "turn-1"
        );
        let notification = server.notifications.recv().await.unwrap();
        assert_eq!(notification.method, "item/agentMessage/delta");
        let approval = server.server_requests.recv().await.unwrap();
        assert_eq!(approval.id, 900);
        server
            .respond(approval.id, Ok(json!({"decision":"decline"})))
            .await
            .unwrap();
        assert_eq!(
            server.notifications.recv().await.unwrap().method,
            "mock/responded"
        );
        let (slow, fast) = tokio::join!(
            server.request("slow", Value::Null),
            server.request("fast", Value::Null)
        );
        assert_eq!(slow.unwrap(), json!("slow"));
        assert_eq!(fast.unwrap(), json!("fast"));
        server.shutdown().await.unwrap();
    }

    #[tokio::test]
    async fn mock_process_reports_timeout_and_crash() {
        let executable = compile_mock_server();
        let server = CodexAppServer::start(mock_config(executable, Duration::from_millis(100)))
            .await
            .unwrap();
        assert!(
            server
                .request("timeout", Value::Null)
                .await
                .unwrap_err()
                .to_string()
                .contains("timed out")
        );
        let _ = server.request("crash", Value::Null).await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(
            server
                .request("after-crash", Value::Null)
                .await
                .unwrap_err()
                .to_string()
                .contains("exited")
        );
    }
}
