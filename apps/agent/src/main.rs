mod backup;
mod codex_bridge;
mod durable_outbox;
mod protocol;
mod sandbox;
mod workspace;

use anyhow::{Context, Result, bail};
use base64::Engine;
use clap::{Parser, Subcommand};
use directories::ProjectDirs;
use ed25519_dalek::{SigningKey, VerifyingKey};
use futures_util::{SinkExt, StreamExt};
use keyring::Entry;
use protocol::{Envelope, PairPayload, RootRequest};
use rand::rngs::OsRng;
use rfd::AsyncFileDialog;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    time::Duration,
};
use tokio_tungstenite::{connect_async, tungstenite::Message};

const SERVICE: &str = "company-ai-agent";

#[derive(Parser)]
#[command(name = "company-agent", about = "企业 AI 编程助手受控本地执行器")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Enroll {
        #[arg(long)]
        server: String,
        #[arg(long)]
        code: String,
        #[arg(long, default_value = "Windows Agent")]
        name: String,
        #[arg(long, help = "Use the legacy /api/agent/ws pairing endpoint")]
        legacy: bool,
    },
    Run,
    Status,
    ConfigureCodex {
        #[arg(
            long,
            help = "Release artifact to verify and install; it is never used in-place"
        )]
        artifact: PathBuf,
        #[arg(long)]
        version: String,
        #[arg(long)]
        sha256: String,
        #[arg(long)]
        schema_version: String,
        #[arg(long)]
        model_catalog_version: String,
        #[arg(long)]
        config_template_version: String,
    },
    UseLegacy,
    #[command(hide = true)]
    ModelToken,
}
#[derive(Serialize, Deserialize, Clone)]
struct Root {
    id: String,
    path: PathBuf,
    label: String,
}
#[derive(Serialize, Deserialize)]
struct Config {
    device_id: String,
    server: String,
    roots: Vec<Root>,
    version: String,
    public_key: String,
    #[serde(default)]
    codex: Option<CodexSettings>,
}

#[derive(Serialize, Deserialize, Clone)]
struct CodexSettings {
    release: codex_bridge::ReleaseManifest,
    #[serde(default = "default_request_timeout_seconds")]
    request_timeout_seconds: u64,
}

fn default_request_timeout_seconds() -> u64 {
    30
}

fn config_path() -> Result<PathBuf> {
    let dirs = ProjectDirs::from("com", "company", "ai-agent").context("无法定位用户配置目录")?;
    fs::create_dir_all(dirs.config_dir())?;
    Ok(dirs.config_dir().join("agent.json"))
}
fn agent_data_dir() -> Result<PathBuf> {
    let dirs = ProjectDirs::from("com", "company", "ai-agent")
        .context("unable to locate agent data directory")?;
    fs::create_dir_all(dirs.data_local_dir())?;
    Ok(dirs.data_local_dir().to_path_buf())
}
fn active_task_path() -> Result<PathBuf> {
    Ok(agent_data_dir()?.join("active-codex-task.json"))
}
fn active_task_lock_path(path: &Path) -> PathBuf {
    path.with_extension("lock")
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
struct ActiveCodexTask {
    task_id: String,
    #[serde(default)]
    daemon_instance: String,
    #[serde(default)]
    daemon_pid: u32,
}

fn read_active_task(path: &Path) -> Result<Option<ActiveCodexTask>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(
            serde_json::from_slice(&bytes).context("active Codex task state is invalid")?,
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}
fn write_active_task(path: &Path, task: &ActiveCodexTask) -> Result<()> {
    let parent = path.parent().context("active task path has no parent")?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!("active-codex-task-{}.tmp", uuid::Uuid::new_v4()));
    fs::write(&temporary, serde_json::to_vec(task)?)?;
    if path.exists() {
        fs::remove_file(path)?;
    }
    fs::rename(temporary, path)?;
    Ok(())
}
fn claim_active_task(path: &Path, task_id: &str, daemon_instance: &str) -> Result<ActiveCodexTask> {
    if let Some(active) = read_active_task(path)? {
        bail!("device already has active Codex task {}", active.task_id)
    }
    let lock_path = active_task_lock_path(path);
    match fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&lock_path)
    {
        Ok(file) => file.sync_all()?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            bail!("device already has an active Codex task")
        }
        Err(error) => return Err(error.into()),
    }
    let state = ActiveCodexTask {
        task_id: task_id.into(),
        daemon_instance: daemon_instance.into(),
        daemon_pid: std::process::id(),
    };
    if let Err(error) = write_active_task(path, &state) {
        let _ = fs::remove_file(lock_path);
        return Err(error);
    }
    Ok(state)
}
#[cfg(windows)]
fn process_is_alive(pid: u32) -> bool {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, WAIT_TIMEOUT},
        System::Threading::{OpenProcess, WaitForSingleObject},
    };
    const SYNCHRONIZE_ACCESS: u32 = 0x0010_0000;
    let handle = unsafe { OpenProcess(SYNCHRONIZE_ACCESS, 0, pid) };
    if handle.is_null() {
        return false;
    }
    let result = unsafe { WaitForSingleObject(handle, 0) };
    unsafe { CloseHandle(handle) };
    result == WAIT_TIMEOUT
}
#[cfg(not(windows))]
fn process_is_alive(_pid: u32) -> bool {
    false
}
fn clear_instance_active_task(path: &Path, daemon_instance: &str) -> Result<()> {
    if let Some(active) = read_active_task(path)? {
        if active.daemon_instance == daemon_instance {
            clear_active_task(path, &active.task_id)?;
        }
    }
    Ok(())
}
fn is_terminal_turn_event(method: &str) -> bool {
    matches!(
        method,
        "turn/completed" | "turn/failed" | "turn/cancelled" | "turn/canceled"
    )
}
fn clear_active_task(path: &Path, task_id: &str) -> Result<()> {
    if read_active_task(path)?.is_some_and(|active| active.task_id == task_id) {
        fs::remove_file(path)?;
        let lock_path = active_task_lock_path(path);
        if lock_path.exists() {
            fs::remove_file(lock_path)?;
        }
    }
    Ok(())
}
fn secret_entry() -> Result<Entry> {
    Ok(Entry::new(SERVICE, "device-secret")?)
}
fn save_config(config: &Config, secret: &str) -> Result<()> {
    fs::write(config_path()?, serde_json::to_vec_pretty(config)?)?;
    secret_entry()?.set_password(secret)?;
    Ok(())
}
fn load_config() -> Result<(Config, String)> {
    let config: Config =
        serde_json::from_slice(&fs::read(config_path().context("请先运行 enroll")?)?)?;
    Ok((config, secret_entry()?.get_password()?))
}
fn websocket_url(server: &str) -> Result<String> {
    let value = server.trim_end_matches('/');
    if value.starts_with("ws://localhost")
        || value.starts_with("ws://127.0.0.1")
        || value.starts_with("wss://")
    {
        Ok(format!("{value}/api/agent/ws"))
    } else {
        bail!("仅允许 localhost 的 ws:// 或生产 wss:// 服务器")
    }
}
fn enrollment_websocket_url(server: &str, legacy: bool) -> Result<String> {
    if legacy {
        websocket_url(server)
    } else {
        codex_websocket_url(server)
    }
}
fn public_key() -> (SigningKey, String) {
    let key = SigningKey::generate(&mut OsRng);
    let public = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .encode(VerifyingKey::from(&key).as_bytes());
    (key, public)
}
async fn enroll(server: String, code: String, name: String, legacy: bool) -> Result<()> {
    let picked = pick_folder_foreground("选择允许 AI Agent 访问的工程根目录").await?;
    let canonical = fs::canonicalize(&picked)?;
    let (_, public_key) = public_key();
    let root = Root {
        id: String::new(),
        label: canonical
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string(),
        path: canonical,
    };
    let enrollment_url = enrollment_websocket_url(&server, legacy)?;
    let (mut socket, _) = connect_async(enrollment_url).await?;
    let payload = PairPayload {
        code,
        name,
        public_key: public_key.clone(),
        version: env!("CARGO_PKG_VERSION").into(),
        roots: vec![RootRequest {
            label: root.label.clone(),
        }],
    };
    socket
        .send(Message::Text(
            serde_json::to_string(&Envelope::pair(payload))?.into(),
        ))
        .await?;
    let response = socket
        .next()
        .await
        .context("服务器未返回配对结果")??
        .into_text()?;
    let result: Envelope = serde_json::from_str(&response)?;
    if result.r#type != "pair_result" {
        bail!("配对失败：{}", result.payload)
    }
    let value: serde_json::Value = serde_json::from_value(result.payload)?;
    let device_id = value["deviceId"]
        .as_str()
        .context("缺少设备 ID")?
        .to_owned();
    let credential = value["credential"]
        .as_str()
        .context("缺少设备凭据")?
        .to_owned();
    let root_id = value["roots"][0]["id"]
        .as_str()
        .context("缺少根目录 ID")?
        .to_owned();
    let config = Config {
        device_id,
        server,
        roots: vec![Root {
            id: root_id,
            ..root
        }],
        version: env!("CARGO_PKG_VERSION").into(),
        public_key,
        codex: None,
    };
    save_config(&config, &credential)?;
    println!("配对成功。运行 `company-agent run` 开始后台连接。");
    Ok(())
}
async fn run() -> Result<()> {
    let (mut config, credential) = load_config()?;
    if let Some(settings) = config.codex.clone() {
        return run_codex(config, credential, settings).await;
    }
    let url = websocket_url(&config.server)?;
    loop {
        match run_connection(&mut config, &credential, &url).await {
            Ok(()) => {}
            Err(error) => eprintln!("连接断开：{error:#}"),
        };
        tokio::time::sleep(Duration::from_secs(5)).await;
    }
}

fn codex_websocket_url(server: &str) -> Result<String> {
    let value = server.trim_end_matches('/');
    if value.starts_with("ws://localhost")
        || value.starts_with("ws://127.0.0.1")
        || value.starts_with("wss://")
    {
        Ok(format!("{value}/ws/devices"))
    } else {
        bail!("Codex bridge only allows localhost ws:// or production wss:// servers")
    }
}

fn http_server_url(server: &str) -> Result<String> {
    let value = server.trim_end_matches('/');
    if let Some(rest) = value.strip_prefix("wss://") {
        return Ok(format!("https://{rest}"));
    }
    if let Some(rest) = value.strip_prefix("ws://localhost") {
        return Ok(format!("http://localhost{rest}"));
    }
    if let Some(rest) = value.strip_prefix("ws://127.0.0.1") {
        return Ok(format!("http://127.0.0.1{rest}"));
    }
    if value.starts_with("https://")
        || value.starts_with("http://localhost")
        || value.starts_with("http://127.0.0.1")
    {
        return Ok(value.into());
    }
    bail!("server must use HTTPS or localhost HTTP")
}

async fn model_token() -> Result<()> {
    let (config, credential) = load_config()?;
    let active = read_active_task(&active_task_path()?)?.context("no active Codex task")?;
    let response = reqwest::Client::new()
        .post(format!("{}/model-tokens", http_server_url(&config.server)?))
        .header("X-Device-ID", &config.device_id)
        .bearer_auth(credential)
        .json(&serde_json::json!({"task_id":active.task_id,"model":"deepseek-v4-flash"}))
        .send()
        .await
        .context("model token request failed")?;
    if !response.status().is_success() {
        bail!("model token request was rejected ({})", response.status())
    }
    let body: serde_json::Value = response
        .json()
        .await
        .context("invalid model token response")?;
    let token = body["access_token"]
        .as_str()
        .context("model token response omitted access_token")?;
    print!("{token}");
    Ok(())
}

async fn run_codex(config: Config, credential: String, settings: CodexSettings) -> Result<()> {
    let data_dir = agent_data_dir()?;
    let current_exe = fs::canonicalize(std::env::current_exe()?)?;
    let runtime = codex_bridge::RuntimeConfig {
        managed_runtime_dir: data_dir.join("runtime"),
        release: settings.release,
        request_timeout: Duration::from_secs(settings.request_timeout_seconds.clamp(1, 300)),
        codex_home: data_dir.join("codex-home"),
        responses_base_url: format!("{}/v1", http_server_url(&config.server)?),
        auth_command: current_exe,
    };
    let daemon_instance = uuid::Uuid::new_v4().to_string();
    let active_path = active_task_path()?;
    let mut outbox = durable_outbox::DurableOutbox::load(data_dir.join("event-outbox.json"))?;
    // A fresh daemon instance never inherits a previous process's task lease.
    if let Some(stale) = read_active_task(&active_path)? {
        if process_is_alive(stale.daemon_pid) {
            bail!("another company-agent process owns the active Codex task lease")
        }
        outbox.ensure_recovery_failure(
            &stale.task_id,
            "daemon stopped before the task reached a terminal event",
        )?;
        clear_active_task(&active_path, &stale.task_id)?;
    }
    let stale_workspaces = data_dir.join("task-workspaces");
    if stale_workspaces.exists() {
        fs::remove_dir_all(&stale_workspaces).context("failed to clean stale task workspaces")?;
    }
    fs::create_dir_all(&stale_workspaces)?;
    let mut retry = Duration::from_secs(1);
    let mut workspaces: BTreeMap<String, workspace::ShadowWorkspace> = BTreeMap::new();
    loop {
        match codex_bridge::CodexAppServer::start(runtime.clone()).await {
            Ok(mut bridge) => {
                retry = Duration::from_secs(1);
                let mut book = codex_bridge::DispatchBook::default();
                while let Err(error) = run_codex_connection(
                    &config,
                    &credential,
                    &daemon_instance,
                    &mut bridge,
                    &mut book,
                    &mut outbox,
                    &mut workspaces,
                )
                .await
                {
                    eprintln!("Codex device connection closed: {error:#}");
                    if !bridge.is_running().await.unwrap_or(false) {
                        break;
                    }
                    tokio::time::sleep(Duration::from_secs(5)).await;
                }
                let _ = bridge.shutdown().await;
                persist_unfinished_tasks(
                    &book,
                    &mut outbox,
                    "Codex App Server stopped before the task reached a terminal event",
                )?;
                workspace::cleanup_all(&mut workspaces)?;
                clear_instance_active_task(&active_path, &daemon_instance)?;
            }
            Err(error) => eprintln!("Codex App Server failed to start: {error:#}"),
        }
        tokio::time::sleep(retry).await;
        retry = (retry * 2).min(Duration::from_secs(60));
    }
}

fn persist_unfinished_tasks(
    book: &codex_bridge::DispatchBook,
    outbox: &mut durable_outbox::DurableOutbox,
    reason: &str,
) -> Result<()> {
    for task_id in book.task_ids() {
        outbox.ensure_recovery_failure(task_id, reason)?;
    }
    Ok(())
}

async fn send_json<S>(write: &mut S, value: serde_json::Value) -> Result<()>
where
    S: futures_util::Sink<Message> + Unpin,
    S::Error: std::error::Error + Send + Sync + 'static,
{
    write
        .send(Message::Text(serde_json::to_string(&value)?.into()))
        .await?;
    Ok(())
}

async fn send_audit_event<S>(
    write: &mut S,
    outbox: &mut durable_outbox::DurableOutbox,
    task_id: &str,
    event_type: &str,
    payload: serde_json::Value,
) -> Result<()>
where
    S: futures_util::Sink<Message> + Unpin,
    S::Error: std::error::Error + Send + Sync + 'static,
{
    let source_event_id = format!("agent:{}", uuid::Uuid::new_v4());
    let payload = workspace::sanitize_audit(payload);
    let event = serde_json::json!({"type":"task.event","task_id":task_id,"source_event_id":source_event_id,"event_type":event_type,"payload":payload});
    persist_then_send(write, outbox, event).await
}

async fn persist_then_send<S>(
    write: &mut S,
    outbox: &mut durable_outbox::DurableOutbox,
    event: serde_json::Value,
) -> Result<()>
where
    S: futures_util::Sink<Message> + Unpin,
    S::Error: std::error::Error + Send + Sync + 'static,
{
    outbox.insert(event.clone())?;
    send_json(write, event).await
}

async fn run_codex_connection(
    config: &Config,
    credential: &str,
    daemon_instance: &str,
    bridge: &mut codex_bridge::CodexAppServer,
    book: &mut codex_bridge::DispatchBook,
    outbox: &mut durable_outbox::DurableOutbox,
    workspaces: &mut BTreeMap<String, workspace::ShadowWorkspace>,
) -> Result<()> {
    let active_path = active_task_path()?;
    let backups = backup::BackupStore::new(agent_data_dir()?.join("task-backups"))?;
    let shadow_base = agent_data_dir()?.join("task-workspaces");
    let url = codex_websocket_url(&config.server)?;
    let (socket, _) = connect_async(&url).await?;
    let (mut write, mut read) = socket.split();
    send_json(
        &mut write,
        serde_json::json!({
            "type": "authenticate",
            "device_id": config.device_id,
            "credential": credential,
            "runtime_version": format!("company-agent/{} codex/{}", env!("CARGO_PKG_VERSION"), codex_bridge::PINNED_PROTOCOL_VERSION),
        }),
    )
    .await?;
    let authenticated = read
        .next()
        .await
        .context("device gateway closed before authentication")??;
    let authenticated: serde_json::Value = serde_json::from_str(authenticated.to_text()?)?;
    if authenticated["type"] != "authenticated" {
        bail!("device gateway rejected Codex bridge authentication")
    }
    for event in outbox.values() {
        send_json(&mut write, event.clone()).await?;
    }
    let mut heartbeat = tokio::time::interval(Duration::from_secs(30));
    loop {
        tokio::select! {
            _ = heartbeat.tick() => {
                send_json(&mut write, serde_json::json!({"type":"heartbeat","runtime_version":config.version})).await?;
            }
            notification = bridge.notifications.recv() => {
                let notification = notification.context("Codex notification stream closed")?;
                let thread_id = notification.params.get("threadId").and_then(serde_json::Value::as_str).unwrap_or("");
                let turn_id = notification.params.get("turnId").and_then(serde_json::Value::as_str).unwrap_or("");
                let Some(task_id) = book.task_for_event(thread_id, turn_id).map(str::to_owned) else { continue };
                let Some(shadow) = workspaces.get(&task_id) else { continue };
                let mut event_type = notification.method.clone();
                let mut payload = workspace::sanitize_event(notification.params, &shadow.path, shadow.real_root());
                let mut sync_audit = None;
                if notification.method == "turn/completed" {
                    match shadow.sync_back(shadow.real_root()) {
                        Ok(()) => sync_audit = Some(("workspace.sync.completed", serde_json::json!({"status":"completed"}))),
                        Err(error) => {
                            event_type = "turn/failed".into();
                            payload = workspace::sanitize_event(serde_json::json!({"error":format!("safe workspace sync failed: {error:#}")}), &shadow.path, shadow.real_root());
                            sync_audit = Some(("workspace.sync.failed", serde_json::json!({"error":error.to_string()})));
                        }
                    }
                }
                let source_event_id = notification.source_event_id.clone();
                let terminal = is_terminal_turn_event(&event_type);
                let event = serde_json::json!({
                    "type":"task.event",
                    "task_id":task_id,
                    "source_event_id":source_event_id,
                    "event_type":event_type,
                    "payload":payload,
                });
                outbox.insert(event.clone())?;
                if terminal {
                    clear_active_task(&active_path, &task_id)?;
                    book.remove(&task_id);
                    if let Some(shadow) = workspaces.remove(&task_id) { let _ = shadow.remove(); }
                    // Command-auth tokens are cached per provider. Restarting the pinned
                    // App Server at each task boundary guarantees no task reuses one.
                    bridge.shutdown().await?;
                    if let Some((audit_type,audit_payload))=sync_audit { send_audit_event(&mut write,outbox,&task_id,audit_type,audit_payload).await?; }
                    send_json(&mut write, event).await?;
                    bail!("Codex task reached terminal state; rotating task-bound model authentication")
                }
                send_json(&mut write, event).await?;
            }
            request = bridge.server_requests.recv() => {
                let request = request.context("Codex server request stream closed")?;
                let thread_id = request.params.get("threadId").and_then(serde_json::Value::as_str).unwrap_or("");
                let turn_id = request.params.get("turnId").and_then(serde_json::Value::as_str).unwrap_or("");
                let Some(task_id) = book.task_for_event(thread_id, turn_id) else {
                    bridge.respond(request.id, Err(anyhow::anyhow!("approval request is not associated with an active task"))).await?;
                    continue;
                };
                let Some(shadow) = workspaces.get(task_id) else {
                    bridge.respond(request.id, Err(anyhow::anyhow!("approval request workspace is unavailable"))).await?;
                    continue;
                };
                let source_event_id = format!("codex-request:{task_id}:{}", request.id);
                let event = serde_json::json!({
                    "type":"task.event",
                    "task_id":task_id,
                    "source_event_id":source_event_id,
                    "event_type":request.method,
                    "payload":{"request_id":request.id,"params":workspace::sanitize_event(request.params, &shadow.path, shadow.real_root())},
                });
                outbox.insert(event.clone())?;
                send_json(&mut write, event).await?;
            }
            message = read.next() => {
                let Some(message) = message else { bail!("device gateway closed") };
                let message = message?;
                if message.is_close() { bail!("device gateway closed") }
                let Message::Text(text) = message else { continue };
                let value: serde_json::Value = serde_json::from_str(&text)?;
                match value["type"].as_str().unwrap_or("") {
                    "heartbeat.ack" | "task.dispatch.acknowledged" => {}
                    "task.event.ack" => {
                        if let Some(source) = value["source_event_id"].as_str() { outbox.remove(source)?; }
                    }
                    "task.dispatch" => {
                        let task_id = value["task_id"].as_str().context("dispatch missing task_id")?;
                        let delivery_id = value["delivery_id"].as_str().context("dispatch missing delivery_id")?;
                        if book.interrupt_params(task_id).is_none() {
                            let root_id = value["root_id"].as_str().context("dispatch missing root_id")?;
                            let prompt = value["prompt"].as_str().context("dispatch missing prompt")?;
                            let canonical = authorized_root(config, root_id)?;
                            claim_active_task(&active_path, task_id, daemon_instance)?;
                            let manifest = match backups.create(task_id, root_id, &canonical) {
                                Ok(manifest) => manifest,
                                Err(error) => {
                                    clear_active_task(&active_path, task_id)?;
                                    send_audit_event(&mut write, outbox, task_id, "backup.failed", serde_json::json!({"root_id":root_id,"error":error.to_string()})).await?;
                                    bail!("task backup failed before turn start: {error:#}")
                                }
                            };
                            send_audit_event(&mut write, outbox, task_id, "backup.created", serde_json::json!({"root_id":root_id,"files":manifest.entries.iter().filter(|entry| entry.kind == backup::EntryKind::File).count(),"snapshot_retention":"retained_until_explicit_cleanup"})).await?;
                            let shadow = match workspace::ShadowWorkspace::create(&shadow_base, task_id, &canonical) { Ok(value)=>value, Err(error)=>{clear_active_task(&active_path,task_id)?;send_audit_event(&mut write,outbox,task_id,"workspace.create.failed",serde_json::json!({"error":error.to_string()})).await?;return Err(error)} };
                            let thread_id = match bridge.start_thread(&shadow.path).await { Ok(value) => value, Err(error) => { clear_active_task(&active_path, task_id)?; let _=shadow.remove(); return Err(error); } };
                            let turn_id = match bridge.start_turn(&thread_id, &shadow.path, prompt).await { Ok(value) => value, Err(error) => { clear_active_task(&active_path, task_id)?; let _=shadow.remove(); return Err(error); } };
                            workspaces.insert(task_id.to_owned(), shadow);
                            book.register(task_id.to_owned(), thread_id, turn_id);
                        }
                        send_json(&mut write, serde_json::json!({"type":"task.dispatch.ack","task_id":task_id,"delivery_id":delivery_id})).await?;
                    }
                    "task.cancel" => {
                        let task_id = value["task_id"].as_str().context("cancel missing task_id")?;
                        if let Some((thread_id, turn_id)) = book.interrupt_params(task_id) { bridge.interrupt(thread_id, turn_id).await?; }
                        clear_active_task(&active_path, task_id)?;
                        book.remove(task_id);
                        if let Some(shadow)=workspaces.remove(task_id){let _=shadow.remove();}
                        bridge.shutdown().await?;
                        bail!("Codex task was cancelled; rotating task-bound model authentication")
                    }
                    "task.rollback" => {
                        let task_id = value["task_id"].as_str().context("rollback missing task_id")?;
                        let root_id = value["root_id"].as_str().context("rollback missing root_id")?;
                        let delivery_id = value["delivery_id"].as_str().context("rollback missing delivery_id")?;
                        if let Some(active) = read_active_task(&active_path)? {
                            send_audit_event(&mut write, outbox, task_id, "rollback.failed", serde_json::json!({"root_id":root_id,"error":format!("Codex task {} is still active", active.task_id)})).await?;
                            send_json(&mut write, serde_json::json!({"type":"task.rollback.ack","task_id":task_id,"delivery_id":delivery_id,"status":"failed"})).await?;
                            continue;
                        }
                        let canonical = match authorized_root(config, root_id) {
                            Ok(root) => root,
                            Err(error) => {
                                send_audit_event(&mut write, outbox, task_id, "rollback.failed", serde_json::json!({"root_id":root_id,"error":error.to_string()})).await?;
                                send_json(&mut write, serde_json::json!({"type":"task.rollback.ack","task_id":task_id,"delivery_id":delivery_id,"status":"failed"})).await?;
                                continue;
                            }
                        };
                        let rollback_status = match backups.rollback(task_id, root_id, &canonical) {
                            Ok(manifest) => {
                                send_audit_event(&mut write, outbox, task_id, "rollback.completed", serde_json::json!({"root_id":root_id,"entries":manifest.entries.len()})).await?;
                                "succeeded"
                            }
                            Err(error) => {
                                send_audit_event(&mut write, outbox, task_id, "rollback.failed", serde_json::json!({"root_id":root_id,"error":error.to_string()})).await?;
                                "failed"
                            }
                        };
                        send_json(&mut write, serde_json::json!({"type":"task.rollback.ack","task_id":task_id,"delivery_id":delivery_id,"status":rollback_status})).await?;
                    }
                    "approval.decision" => {
                        let request_id = value["request_id"].as_u64().context("approval decision missing request_id")?;
                        let delivery_id = value["delivery_id"].as_str().context("approval decision missing delivery_id")?;
                        if value["approved"].as_bool().unwrap_or(false) {
                            bridge.respond(request_id, Ok(value.get("result").cloned().unwrap_or_else(|| serde_json::json!({"decision":"accept"})))).await?;
                        } else {
                            bridge.respond(request_id, Ok(value.get("result").cloned().unwrap_or_else(|| serde_json::json!({"decision":"decline"})))).await?;
                        }
                        send_json(&mut write, serde_json::json!({
                            "type":"approval.decision.ack",
                            "approval_id":value["approval_id"],
                            "task_id":value["task_id"],
                            "delivery_id":delivery_id,
                        })).await?;
                    }
                    _ => {}
                }
            }
        }
    }
}

fn authorized_root(config: &Config, root_id: &str) -> Result<PathBuf> {
    let root = config
        .roots
        .iter()
        .find(|root| root.id == root_id)
        .context("dispatch selected an unauthorized root_id")?;
    let canonical = fs::canonicalize(&root.path).context("authorized root is unavailable")?;
    if canonical != root.path {
        bail!("authorized root changed since enrollment")
    }
    Ok(canonical)
}
async fn run_connection(config: &mut Config, credential: &str, url: &str) -> Result<()> {
    let (socket, _) = connect_async(url).await?;
    println!("已连接到 {url}，设备 ID：{}", config.device_id);
    let (mut write, mut read) = socket.split();
    let rules: Vec<serde_json::Value> = config
        .roots
        .iter()
        .map(|root| serde_json::json!({"rootId": root.id, "content": read_project_rules(root)}))
        .collect();
    write
        .send(Message::Text(
            serde_json::to_string(&Envelope::hello(
                &config.device_id,
                credential,
                &config.version,
                serde_json::Value::Array(rules),
            ))?
            .into(),
        ))
        .await?;
    let mut heartbeat = tokio::time::interval(Duration::from_secs(30));
    loop {
        tokio::select! { _=heartbeat.tick()=> { write.send(Message::Text(serde_json::to_string(&Envelope::heartbeat(&config.device_id,credential))?.into())).await?; }, message=read.next()=> { let Some(message)=message else { println!("服务器关闭连接"); return Ok(()) }; let message=message?; let Message::Text(text)=message else { if message.is_close() { return Ok(()); } continue; }; let envelope:Envelope=serde_json::from_str(&text)?; if envelope.r#type=="task" { let task_id=envelope.task_id.clone().context("任务缺少 ID")?; println!("收到任务: {} kind={}",task_id,envelope.payload["kind"].as_str().unwrap_or("?")); let status=handle_task(config,&envelope).await; let response=match status { Ok(payload)=>{ if envelope.payload["kind"].as_str()==Some("select_root") { println!("select_root 完成，新目录标签: {}", payload["label"]); } Envelope::task_result(&config.device_id,&task_id,&credential,payload) }, Err(error)=>{ eprintln!("任务 {} 执行失败: {error:#}",task_id); Envelope::task_error(&config.device_id,&task_id,&credential,error.to_string()) } }; write.send(Message::Text(serde_json::to_string(&response)?.into())).await?; } } }
    }
}
async fn handle_task(config: &mut Config, envelope: &Envelope) -> Result<serde_json::Value> {
    if envelope.payload["kind"].as_str() == Some("select_root") {
        let root_id = envelope.payload["rootId"].as_str().context("缺少 rootId")?;
        let path =
            fs::canonicalize(pick_folder_foreground("更换 AI Agent 允许访问的工程目录").await?)?;
        let (label, rules) = {
            let root = config
                .roots
                .iter_mut()
                .find(|root| root.id == root_id)
                .context("未授权的项目根目录")?;
            root.path = path;
            root.label = root
                .path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();
            (root.label.clone(), read_project_rules(root))
        };
        fs::write(config_path()?, serde_json::to_vec_pretty(config)?)?;
        return Ok(serde_json::json!({"rootId":root_id,"label":label,"rules":rules}));
    }
    sandbox::execute(&config.roots, &envelope.payload).await
}

fn read_project_rules(root: &Root) -> String {
    const LIMIT: usize = 64 * 1024;
    match fs::read(root.path.join("AGENTS.md")) {
        Ok(bytes) => {
            let trimmed = if bytes.len() > LIMIT {
                &bytes[..LIMIT]
            } else {
                &bytes[..]
            };
            String::from_utf8_lossy(trimmed).into_owned()
        }
        Err(_) => String::new(),
    }
}

#[cfg(windows)]
fn bring_picker_to_front() {
    // 仅靠 pump_foreground 置顶文件对话框；绝不显示/操作控制台窗口，
    // 否则会弹出 company-agent.exe 窗口，关闭它会直接终止 Agent 进程。
}

#[cfg(not(windows))]
fn bring_picker_to_front() {}

#[cfg(windows)]
fn pump_foreground() {
    use windows_sys::Win32::System::Console::GetConsoleWindow;
    use windows_sys::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        BringWindowToTop, EnumWindows, GetForegroundWindow, GetWindowThreadProcessId, HWND_TOPMOST,
        IsWindowVisible, SW_RESTORE, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW, SetForegroundWindow,
        SetWindowPos, ShowWindow,
    };
    unsafe extern "system" fn find_dialog(hwnd: *mut core::ffi::c_void, lparam: isize) -> i32 {
        unsafe {
            let mut pid: u32 = 0;
            GetWindowThreadProcessId(hwnd, &mut pid);
            if pid != lparam as u32 || IsWindowVisible(hwnd) == 0 || hwnd == GetConsoleWindow() {
                return 1;
            }
            ShowWindow(hwnd, SW_RESTORE);
            SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            );
            let foreground = GetForegroundWindow();
            let mut foreground_thread: u32 = 0;
            GetWindowThreadProcessId(foreground, &mut foreground_thread);
            let our_thread = GetCurrentThreadId();
            AttachThreadInput(our_thread, foreground_thread, 1);
            SetForegroundWindow(hwnd);
            BringWindowToTop(hwnd);
            AttachThreadInput(our_thread, foreground_thread, 0);
        }
        1
    }
    unsafe {
        EnumWindows(Some(find_dialog), std::process::id() as isize);
    }
}

#[cfg(not(windows))]
fn pump_foreground() {}

#[cfg(windows)]
fn restore_notopmost() {
    use windows_sys::Win32::System::Console::GetConsoleWindow;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetWindowThreadProcessId, HWND_NOTOPMOST, SWP_NOMOVE, SWP_NOSIZE, SetWindowPos,
    };
    unsafe extern "system" fn clear_topmost(hwnd: *mut core::ffi::c_void, lparam: isize) -> i32 {
        unsafe {
            let mut pid: u32 = 0;
            GetWindowThreadProcessId(hwnd, &mut pid);
            if pid == lparam as u32 && hwnd != GetConsoleWindow() {
                SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            }
        }
        1
    }
    unsafe {
        EnumWindows(Some(clear_topmost), std::process::id() as isize);
    }
}

#[cfg(not(windows))]
fn restore_notopmost() {}

#[cfg(windows)]
fn guard_console_close() {
    use windows_sys::Win32::System::Console::SetConsoleCtrlHandler;
    unsafe extern "system" fn ignore(_: u32) -> i32 {
        1
    }
    unsafe {
        // 忽略控制台关闭事件，防止误关 Agent 窗口导致进程退出。
        SetConsoleCtrlHandler(Some(ignore), 1);
    }
}

#[cfg(not(windows))]
fn guard_console_close() {}

#[cfg(windows)]
async fn pick_folder_foreground(title: &str) -> Result<PathBuf> {
    bring_picker_to_front();
    let picker = AsyncFileDialog::new().set_title(title).pick_folder();
    tokio::pin!(picker);
    let mut pump = tokio::time::interval(Duration::from_millis(100));
    let result = loop {
        tokio::select! {
            result = &mut picker => break result,
            _ = pump.tick() => pump_foreground(),
        }
    };
    restore_notopmost();
    result
        .map(|handle| handle.path().to_path_buf())
        .context("未选择目录")
}

#[cfg(not(windows))]
async fn pick_folder_foreground(title: &str) -> Result<PathBuf> {
    let picked = AsyncFileDialog::new()
        .set_title(title)
        .pick_folder()
        .await
        .context("未选择目录")?;
    Ok(picked.path().to_path_buf())
}
fn status() -> Result<()> {
    let (config, _) = load_config()?;
    println!(
        "设备：{}\n服务器：{}\n根目录：{}",
        config.device_id,
        config.server,
        config
            .roots
            .iter()
            .map(|r| r.path.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    );
    Ok(())
}

fn configure_codex(artifact: PathBuf, release: codex_bridge::ReleaseManifest) -> Result<()> {
    let (mut config, _) = load_config()?;
    let settings = CodexSettings {
        release: release.clone(),
        request_timeout_seconds: default_request_timeout_seconds(),
    };
    let data_dir = agent_data_dir()?;
    codex_bridge::install_release(&data_dir.join("runtime"), &artifact, &release)?;
    codex_bridge::RuntimeConfig {
        managed_runtime_dir: data_dir.join("runtime"),
        release,
        request_timeout: Duration::from_secs(settings.request_timeout_seconds),
        codex_home: data_dir.join("codex-home"),
        responses_base_url: format!("{}/v1", http_server_url(&config.server)?),
        auth_command: fs::canonicalize(std::env::current_exe()?)?,
    }
    .validate()?;
    config.codex = Some(settings);
    fs::write(config_path()?, serde_json::to_vec_pretty(&config)?)?;
    println!("Codex bridge configured; restart company-agent run to activate it.");
    Ok(())
}

fn use_legacy() -> Result<()> {
    let path = config_path()?;
    let mut value: serde_json::Value = serde_json::from_slice(&fs::read(&path)?)?;
    value["codex"] = serde_json::Value::Null;
    fs::write(path, serde_json::to_vec_pretty(&value)?)?;
    println!("Legacy executor selected; restart company-agent run to activate it.");
    Ok(())
}
#[tokio::main]
async fn main() -> Result<()> {
    guard_console_close();
    let cli = Cli::parse();
    match cli.command {
        Command::Enroll {
            server,
            code,
            name,
            legacy,
        } => enroll(server, code, name, legacy).await,
        Command::Run => run().await,
        Command::Status => status(),
        Command::ConfigureCodex {
            artifact,
            version,
            sha256,
            schema_version,
            model_catalog_version,
            config_template_version,
        } => configure_codex(
            artifact,
            codex_bridge::ReleaseManifest {
                version,
                sha256,
                schema_version,
                model_catalog_version,
                config_template_version,
            },
        ),
        Command::UseLegacy => use_legacy(),
        Command::ModelToken => model_token().await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FailingSink;
    impl futures_util::Sink<Message> for FailingSink {
        type Error = std::io::Error;
        fn poll_ready(
            self: std::pin::Pin<&mut Self>,
            _context: &mut std::task::Context<'_>,
        ) -> std::task::Poll<std::result::Result<(), Self::Error>> {
            std::task::Poll::Ready(Err(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "injected socket failure",
            )))
        }
        fn start_send(
            self: std::pin::Pin<&mut Self>,
            _item: Message,
        ) -> std::result::Result<(), Self::Error> {
            unreachable!("poll_ready always fails")
        }
        fn poll_flush(
            self: std::pin::Pin<&mut Self>,
            _context: &mut std::task::Context<'_>,
        ) -> std::task::Poll<std::result::Result<(), Self::Error>> {
            std::task::Poll::Ready(Ok(()))
        }
        fn poll_close(
            self: std::pin::Pin<&mut Self>,
            _context: &mut std::task::Context<'_>,
        ) -> std::task::Poll<std::result::Result<(), Self::Error>> {
            std::task::Poll::Ready(Ok(()))
        }
    }

    #[test]
    fn main_source_contains_no_known_mojibake_markers() {
        let source = include_str!("main.rs");
        for marker in [
            "\u{951b}",
            "\u{9286}",
            "\u{9225}",
            "\u{9983}",
            "\u{6d7c}\u{4f7b}\u{4e7f}",
            "\u{93c3}\u{72b3}\u{6ccb}\u{7eeb}",
        ] {
            assert!(!source.contains(marker), "mojibake marker found: {marker}");
        }
    }
    use serde_json::json;

    #[test]
    fn task_result_carries_credential() {
        let envelope = Envelope::task_result(
            "device-1",
            "task-1",
            "secret",
            json!({"rootId": "root-1", "label": "新目录"}),
        );
        assert_eq!(envelope.r#type, "task_result");
        assert_eq!(envelope.payload["status"], "completed");
        assert_eq!(envelope.payload["credential"], "secret");
        assert_eq!(envelope.payload["result"]["label"], "新目录");
        assert_eq!(envelope.task_id.as_deref(), Some("task-1"));
    }

    #[test]
    fn task_error_carries_credential() {
        let envelope = Envelope::task_error("device-1", "task-1", "secret", "boom".into());
        assert_eq!(envelope.payload["status"], "failed");
        assert_eq!(envelope.payload["credential"], "secret");
        assert_eq!(envelope.payload["error"], "boom");
    }

    #[test]
    fn reads_project_rules_from_agents_md() {
        let dir = std::env::temp_dir().join(format!("agent-rules-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let root = Root {
            id: "root".into(),
            path: dir.clone(),
            label: "test".into(),
        };
        assert_eq!(read_project_rules(&root), "");
        fs::write(dir.join("AGENTS.md"), "# 团队规范\n- 使用中文").unwrap();
        assert_eq!(read_project_rules(&root), "# 团队规范\n- 使用中文");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn codex_dispatch_accepts_only_an_enrolled_root_id() {
        let dir = std::env::temp_dir().join(format!("agent-codex-root-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let canonical = fs::canonicalize(&dir).unwrap();
        let config = Config {
            device_id: "device".into(),
            server: "ws://localhost:8081".into(),
            roots: vec![Root {
                id: "allowed".into(),
                path: canonical.clone(),
                label: "test".into(),
            }],
            version: "test".into(),
            public_key: "test".into(),
            codex: None,
        };
        assert_eq!(authorized_root(&config, "allowed").unwrap(), canonical);
        assert!(authorized_root(&config, "other").is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn active_codex_task_is_exclusive_and_cleared_by_owner() {
        let dir = std::env::temp_dir().join(format!("agent-active-task-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("active.json");
        assert_eq!(
            claim_active_task(&path, "task-1", "daemon-1")
                .unwrap()
                .task_id,
            "task-1"
        );
        assert!(claim_active_task(&path, "task-2", "daemon-1").is_err());
        clear_active_task(&path, "task-2").unwrap();
        assert_eq!(read_active_task(&path).unwrap().unwrap().task_id, "task-1");
        clear_active_task(&path, "task-1").unwrap();
        assert!(read_active_task(&path).unwrap().is_none());
        assert!(!active_task_lock_path(&path).exists());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn bridge_crash_persists_failure_but_normal_terminal_does_not() {
        let dir = std::env::temp_dir().join(format!("agent-outbox-{}", uuid::Uuid::new_v4()));
        let path = dir.join("event-outbox.json");
        let mut outbox = durable_outbox::DurableOutbox::load(path.clone()).unwrap();
        let mut book = codex_bridge::DispatchBook::default();
        book.register("crashed".into(), "thread".into(), "turn".into());
        persist_unfinished_tasks(&book, &mut outbox, "bridge crashed").unwrap();
        assert!(outbox.has_terminal("crashed"));

        let mut normal = codex_bridge::DispatchBook::default();
        normal.register("completed".into(), "thread".into(), "turn".into());
        outbox
            .insert(json!({"type":"task.event","task_id":"completed","source_event_id":"completed-event","event_type":"turn/completed","payload":{}}))
            .unwrap();
        normal.remove("completed");
        persist_unfinished_tasks(&normal, &mut outbox, "bridge stopped").unwrap();
        assert_eq!(
            outbox
                .values()
                .filter(|event| event["task_id"] == "completed")
                .count(),
            1
        );
        assert_eq!(
            durable_outbox::DurableOutbox::load(path)
                .unwrap()
                .values()
                .filter(|event| event["task_id"] == "crashed")
                .count(),
            1
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn failed_socket_send_leaves_event_in_durable_outbox() {
        let dir = std::env::temp_dir().join(format!("agent-outbox-{}", uuid::Uuid::new_v4()));
        let path = dir.join("event-outbox.json");
        let mut outbox = durable_outbox::DurableOutbox::load(path.clone()).unwrap();
        let mut sink = FailingSink;
        let event = json!({"type":"task.event","task_id":"task","source_event_id":"send-fails","event_type":"text.delta","payload":{}});
        assert!(
            persist_then_send(&mut sink, &mut outbox, event)
                .await
                .is_err()
        );
        assert_eq!(
            durable_outbox::DurableOutbox::load(path)
                .unwrap()
                .values()
                .next()
                .unwrap()["source_event_id"],
            "send-fails"
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn converts_only_allowed_server_urls_for_http() {
        assert_eq!(
            http_server_url("wss://agent.example").unwrap(),
            "https://agent.example"
        );
        assert_eq!(
            http_server_url("ws://localhost:8000").unwrap(),
            "http://localhost:8000"
        );
        assert!(http_server_url("http://agent.example").is_err());
    }

    #[test]
    fn enroll_defaults_to_target_gateway_and_legacy_is_explicit() {
        assert_eq!(
            enrollment_websocket_url("wss://agent.example", false).unwrap(),
            "wss://agent.example/ws/devices"
        );
        assert_eq!(
            enrollment_websocket_url("wss://agent.example", true).unwrap(),
            "wss://agent.example/api/agent/ws"
        );
    }

    #[test]
    fn all_turn_terminal_events_rotate_the_app_server() {
        for method in [
            "turn/completed",
            "turn/failed",
            "turn/cancelled",
            "turn/canceled",
        ] {
            assert!(is_terminal_turn_event(method));
        }
        assert!(!is_terminal_turn_event("item/agentMessage/delta"));
    }
}
