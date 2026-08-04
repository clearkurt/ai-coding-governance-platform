# Rust 本地 Agent

`company-agent` 是受控的 Windows 本地执行器：它只会通过反向 WebSocket 接收任务，并且只可访问工程师在配对时本地选择的目录。

## 开发构建

本机使用 Rust GNU 目标（因为开发机未安装 MSVC Build Tools）：

```powershell
cd apps/agent
$env:PATH = "C:\msys64\ucrt64\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
cargo +stable-x86_64-pc-windows-gnu test
cargo +stable-x86_64-pc-windows-gnu build --release
```

产物为 `target/x86_64-pc-windows-gnu/release/company-agent.exe` 与 `agent-updater.exe`。

## 本地配对

1. 登录网页，在“本地 Agent”区创建一次性配对码。
2. 执行：

```powershell
company-agent.exe enroll --server ws://localhost:3000 --code <配对码>
company-agent.exe run
```

配对时会弹出目录选择窗口；Agent 凭据保存到 Windows Credential Manager，配置文件不保存明文凭据。

`ws://` 只允许 `localhost`/`127.0.0.1`，内网部署必须使用 `wss://`。

## Codex App Server 模式

新模式只接受产品分发的固定 Codex 可执行文件绝对路径，不从 `PATH` 或 WindowsApps 商店目录启动。配置前可同时提供发布清单中的 SHA-256：

```powershell
company-agent.exe configure-codex --executable C:\ProgramData\CompanyAgent\runtime\codex.exe --sha256 <发布哈希>
company-agent.exe run
```

守护进程会校验固定版本，通过 `app-server --stdio --strict-config` 启动子进程，并用 Windows Job Object 保证退出时终止整个进程树。任务中的 `root_id` 必须匹配配对时授权的根目录。需要临时回退旧执行器时运行：

```powershell
company-agent.exe use-legacy
```
