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
use rfd::FileDialog;
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
async fn enroll(server: String, code: String, name: String) -> Result<()> { let directory=FileDialog::new().set_title("选择允许 AI Agent 访问的工程根目录").pick_folder().context("未选择工程目录")?; let canonical=fs::canonicalize(&directory)?; let (_, public_key)=public_key(); let root=Root { id: String::new(), label: canonical.file_name().unwrap_or_default().to_string_lossy().to_string(), path: canonical }; let (mut socket,_)=connect_async(websocket_url(&server)?).await?; let payload=PairPayload { code, name, public_key: public_key.clone(), version: env!("CARGO_PKG_VERSION").into(), roots: vec![RootRequest { label: root.label.clone() }] }; socket.send(Message::Text(serde_json::to_string(&Envelope::pair(payload))?.into())).await?; let response=socket.next().await.context("服务器未返回配对结果")??.into_text()?; let result:Envelope=serde_json::from_str(&response)?; if result.r#type!="pair_result" { bail!("配对失败：{}", result.payload) } let value:serde_json::Value=serde_json::from_value(result.payload)?; let device_id=value["deviceId"].as_str().context("缺少设备 ID")?.to_owned(); let credential=value["credential"].as_str().context("缺少设备凭据")?.to_owned(); let root_id=value["roots"][0]["id"].as_str().context("缺少根目录 ID")?.to_owned(); let config=Config { device_id, server, roots: vec![Root { id:root_id, ..root }], version:env!("CARGO_PKG_VERSION").into(), public_key }; save_config(&config,&credential)?; println!("配对成功。运行 `company-agent run` 开始后台连接。"); Ok(()) }
async fn run() -> Result<()> { let (config, credential)=load_config()?; let url=websocket_url(&config.server)?; loop { match run_connection(&config,&credential,&url).await { Ok(()) => {}, Err(error) => eprintln!("连接断开：{error:#}") }; tokio::time::sleep(Duration::from_secs(5)).await; } }
async fn run_connection(config:&Config, credential:&str, url:&str) -> Result<()> { let (socket,_)=connect_async(url).await?; let (mut write,mut read)=socket.split(); write.send(Message::Text(serde_json::to_string(&Envelope::hello(&config.device_id,credential,&config.version))?.into())).await?; let mut heartbeat=tokio::time::interval(Duration::from_secs(30)); loop { tokio::select! { _=heartbeat.tick()=> { write.send(Message::Text(serde_json::to_string(&Envelope::heartbeat(&config.device_id,credential))?.into())).await?; }, message=read.next()=> { let Some(message)=message else { return Ok(()) }; let envelope:Envelope=serde_json::from_str(&message?.into_text()?)?; if envelope.r#type=="task" { let task_id=envelope.task_id.clone().context("任务缺少 ID")?; let status=handle_task(config,&envelope).await; let response=match status { Ok(payload)=>Envelope::task_result(&config.device_id,&task_id,payload), Err(error)=>Envelope::task_error(&config.device_id,&task_id,error.to_string()) }; write.send(Message::Text(serde_json::to_string(&response)?.into())).await?; } } } } }
async fn handle_task(config:&Config, envelope:&Envelope) -> Result<serde_json::Value> { sandbox::execute(&config.roots,&envelope.payload) }
fn status() -> Result<()> { let (config,_)=load_config()?; println!("设备：{}\n服务器：{}\n根目录：{}",config.device_id,config.server,config.roots.iter().map(|r|r.path.display().to_string()).collect::<Vec<_>>().join(", ")); Ok(()) }
#[tokio::main]
async fn main() -> Result<()> { let cli=Cli::parse(); match cli.command { Command::Enroll{server,code,name}=>enroll(server,code,name).await, Command::Run=>run().await, Command::Status=>status() } }
