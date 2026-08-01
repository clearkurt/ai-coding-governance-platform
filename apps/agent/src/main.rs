mod protocol;
mod sandbox;

use anyhow::{bail, Context, Result};
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
use std::{fs, path::PathBuf, time::Duration};
use tokio_tungstenite::{connect_async, tungstenite::Message};

const SERVICE: &str = "company-ai-agent";

#[derive(Parser)]
#[command(name="company-agent", about="企业 AI 编程助手受控本地执行器")]
struct Cli { #[command(subcommand)] command: Command }
#[derive(Subcommand)]
enum Command { Enroll { #[arg(long)] server: String, #[arg(long)] code: String, #[arg(long, default_value="Windows Agent")] name: String }, Run, Status }
#[derive(Serialize, Deserialize, Clone)]
struct Root { id: String, path: PathBuf, label: String }
#[derive(Serialize, Deserialize)]
struct Config { device_id: String, server: String, roots: Vec<Root>, version: String, public_key: String }

fn config_path() -> Result<PathBuf> { let dirs=ProjectDirs::from("com","company","ai-agent").context("无法定位用户配置目录")?; fs::create_dir_all(dirs.config_dir())?; Ok(dirs.config_dir().join("agent.json")) }
fn secret_entry() -> Result<Entry> { Ok(Entry::new(SERVICE, "device-secret")?) }
fn save_config(config: &Config, secret: &str) -> Result<()> { fs::write(config_path()?, serde_json::to_vec_pretty(config)?)?; secret_entry()?.set_password(secret)?; Ok(()) }
fn load_config() -> Result<(Config,String)> { let config:Config=serde_json::from_slice(&fs::read(config_path().context("请先运行 enroll")?)?)?; Ok((config,secret_entry()?.get_password()?)) }
fn websocket_url(server: &str) -> Result<String> { let value=server.trim_end_matches('/'); if value.starts_with("ws://localhost") || value.starts_with("ws://127.0.0.1") || value.starts_with("wss://") { Ok(format!("{value}/api/agent/ws")) } else { bail!("仅允许 localhost 的 ws:// 或生产 wss:// 服务器") } }
fn public_key() -> (SigningKey,String) { let key=SigningKey::generate(&mut OsRng); let public=base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(VerifyingKey::from(&key).as_bytes()); (key,public) }
async fn enroll(server: String, code: String, name: String) -> Result<()> { let picked=pick_folder_foreground("选择允许 AI Agent 访问的工程根目录").await?; let canonical=fs::canonicalize(&picked)?; let (_, public_key)=public_key(); let root=Root { id: String::new(), label: canonical.file_name().unwrap_or_default().to_string_lossy().to_string(), path: canonical }; let (mut socket,_)=connect_async(websocket_url(&server)?).await?; let payload=PairPayload { code, name, public_key: public_key.clone(), version: env!("CARGO_PKG_VERSION").into(), roots: vec![RootRequest { label: root.label.clone() }] }; socket.send(Message::Text(serde_json::to_string(&Envelope::pair(payload))?.into())).await?; let response=socket.next().await.context("服务器未返回配对结果")??.into_text()?; let result:Envelope=serde_json::from_str(&response)?; if result.r#type!="pair_result" { bail!("配对失败：{}", result.payload) } let value:serde_json::Value=serde_json::from_value(result.payload)?; let device_id=value["deviceId"].as_str().context("缺少设备 ID")?.to_owned(); let credential=value["credential"].as_str().context("缺少设备凭据")?.to_owned(); let root_id=value["roots"][0]["id"].as_str().context("缺少根目录 ID")?.to_owned(); let config=Config { device_id, server, roots: vec![Root { id:root_id, ..root }], version:env!("CARGO_PKG_VERSION").into(), public_key }; save_config(&config,&credential)?; println!("配对成功。运行 `company-agent run` 开始后台连接。"); Ok(()) }
async fn run() -> Result<()> { let (mut config, credential)=load_config()?; let url=websocket_url(&config.server)?; loop { match run_connection(&mut config,&credential,&url).await { Ok(()) => {}, Err(error) => eprintln!("连接断开：{error:#}") }; tokio::time::sleep(Duration::from_secs(5)).await; } }
async fn run_connection(config:&mut Config, credential:&str, url:&str) -> Result<()> { let (socket,_)=connect_async(url).await?; println!("已连接到 {url}，设备 ID：{}", config.device_id); let (mut write,mut read)=socket.split(); write.send(Message::Text(serde_json::to_string(&Envelope::hello(&config.device_id,credential,&config.version))?.into())).await?; let mut heartbeat=tokio::time::interval(Duration::from_secs(30)); loop { tokio::select! { _=heartbeat.tick()=> { write.send(Message::Text(serde_json::to_string(&Envelope::heartbeat(&config.device_id,credential))?.into())).await?; }, message=read.next()=> { let Some(message)=message else { println!("服务器关闭连接"); return Ok(()) }; let message=message?; let Message::Text(text)=message else { if message.is_close() { return Ok(()); } continue; }; let envelope:Envelope=serde_json::from_str(&text)?; if envelope.r#type=="task" { let task_id=envelope.task_id.clone().context("任务缺少 ID")?; println!("收到任务: {} kind={}",task_id,envelope.payload["kind"].as_str().unwrap_or("?")); let status=handle_task(config,&envelope).await; let response=match status { Ok(payload)=>{ if envelope.payload["kind"].as_str()==Some("select_root") { println!("select_root 完成，新目录标签: {}", payload["label"]); } Envelope::task_result(&config.device_id,&task_id,&credential,payload) }, Err(error)=>{ eprintln!("任务 {} 执行失败: {error:#}",task_id); Envelope::task_error(&config.device_id,&task_id,&credential,error.to_string()) } }; write.send(Message::Text(serde_json::to_string(&response)?.into())).await?; } } } } }
async fn handle_task(config:&mut Config, envelope:&Envelope) -> Result<serde_json::Value> { if envelope.payload["kind"].as_str()==Some("select_root") { let root_id=envelope.payload["rootId"].as_str().context("缺少 rootId")?; let path=fs::canonicalize(pick_folder_foreground("更换 AI Agent 允许访问的工程目录").await?)?; let label={ let root=config.roots.iter_mut().find(|root|root.id==root_id).context("未授权的项目根目录")?; root.path=path; root.label= root.path.file_name().unwrap_or_default().to_string_lossy().to_string(); root.label.clone() }; fs::write(config_path()?,serde_json::to_vec_pretty(config)?)?; return Ok(serde_json::json!({"rootId":root_id,"label":label})); } sandbox::execute(&config.roots,&envelope.payload) }

#[cfg(windows)]
fn bring_picker_to_front() {
    use windows_sys::Win32::System::Console::GetConsoleWindow;
    use windows_sys::Win32::UI::WindowsAndMessaging::{SetForegroundWindow, ShowWindow, SW_RESTORE, SW_SHOW};
    unsafe {
        let hwnd = GetConsoleWindow();
        if !hwnd.is_null() {
            ShowWindow(hwnd, SW_RESTORE);
            ShowWindow(hwnd, SW_SHOW);
            SetForegroundWindow(hwnd);
        }
    }
}

#[cfg(not(windows))]
fn bring_picker_to_front() {}

#[cfg(windows)]
fn pump_foreground() {
    use windows_sys::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
    use windows_sys::Win32::UI::WindowsAndMessaging::{BringWindowToTop, EnumWindows, GetForegroundWindow, GetWindowThreadProcessId, IsWindowVisible, SetForegroundWindow, SetWindowPos, ShowWindow, HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW, SW_RESTORE};
    unsafe extern "system" fn find_dialog(hwnd: *mut core::ffi::c_void, lparam: isize) -> i32 {
        unsafe {
            let mut pid: u32 = 0;
            GetWindowThreadProcessId(hwnd, &mut pid);
            if pid != lparam as u32 || IsWindowVisible(hwnd) == 0 { return 1; }
            ShowWindow(hwnd, SW_RESTORE);
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
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
    use windows_sys::Win32::UI::WindowsAndMessaging::{EnumWindows, GetWindowThreadProcessId, SetWindowPos, HWND_NOTOPMOST, SWP_NOMOVE, SWP_NOSIZE};
    unsafe extern "system" fn clear_topmost(hwnd: *mut core::ffi::c_void, lparam: isize) -> i32 {
        unsafe {
            let mut pid: u32 = 0;
            GetWindowThreadProcessId(hwnd, &mut pid);
            if pid == lparam as u32 {
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
    result.map(|handle| handle.path().to_path_buf()).context("未选择目录")
}

#[cfg(not(windows))]
async fn pick_folder_foreground(title: &str) -> Result<PathBuf> {
    let picked = AsyncFileDialog::new().set_title(title).pick_folder().await.context("未选择目录")?;
    Ok(picked.path().to_path_buf())
}
fn status() -> Result<()> { let (config,_)=load_config()?; println!("设备：{}\n服务器：{}\n根目录：{}",config.device_id,config.server,config.roots.iter().map(|r|r.path.display().to_string()).collect::<Vec<_>>().join(", ")); Ok(()) }
#[tokio::main]
async fn main() -> Result<()> { let cli=Cli::parse(); match cli.command { Command::Enroll{server,code,name}=>enroll(server,code,name).await, Command::Run=>run().await, Command::Status=>status() } }

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn task_result_carries_credential() {
        let envelope = Envelope::task_result("device-1", "task-1", "secret", json!({"rootId": "root-1", "label": "新目录"}));
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
}
