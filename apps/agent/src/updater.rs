use anyhow::{bail, Context, Result};
use clap::Parser;
use sha2::{Digest, Sha256};
use std::{fs, path::PathBuf, process::Command};

#[derive(Parser)] struct Args { #[arg(long)] current:PathBuf, #[arg(long)] downloaded:PathBuf, #[arg(long)] expected_sha256:String }
fn main() -> Result<()> { let args=Args::parse(); let bytes=fs::read(&args.downloaded)?; let actual=format!("{:x}",Sha256::digest(&bytes)); if actual!=args.expected_sha256 { bail!("更新包哈希不匹配") } let backup=args.current.with_extension("exe.previous"); if args.current.exists(){fs::rename(&args.current,&backup)?} fs::rename(&args.downloaded,&args.current).context("替换 Agent 失败")?; Command::new(&args.current).arg("run").spawn()?; Ok(()) }
