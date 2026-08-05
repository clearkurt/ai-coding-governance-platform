# 企业内部 AI 编程助手平台

本仓库正在从“中央自研 Agent loop + Windows Rust 执行器”迁移到“本地 Codex App Server + DeepSeek Responses API + 企业控制平面”。目标实现位于 `apps/web-next`、`apps/api-next` 和 `apps/agent`；旧 React/Fastify/SQLite 链路继续作为默认入口和回滚路径。本项目尚未达到生产切换条件。

## 目标技术栈

- Web：Vue 3、TypeScript、Vite、Pinia、Vue Router、Naive UI
- 中央 API：Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2 async、Alembic、HTTPX
- 数据库：PostgreSQL
- 浏览器事件：SSE；命令和审批：HTTP
- 设备通道：经过身份校验的 WSS
- 本地控制：Rust 守护进程通过 stdio JSON-RPC 托管固定版本 Codex App Server
- 模型：公司 Responses 代理后的 DeepSeek `deepseek-v4-flash`

安全边界与目标设计见 [Codex + DeepSeek 目标架构](docs/codex-deepseek-target.md)，验收证据和生产门槛见 [灰度与验收](docs/rollout-acceptance.md)。

## 新链路开发与验证

FastAPI：

```powershell
cd apps/api-next
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
pytest
ruff check .
```

Vue：

```powershell
cd apps/web-next
npm install
npm test
npm run build
npm run test:e2e
```

Windows 守护进程：

```powershell
cd apps/agent
$env:PATH = "C:\msys64\ucrt64\bin;$env:USERPROFILE\.cargo\bin;$env:PATH"
cargo test
cargo run -- run
```

## 旧链路仍是默认入口

仓库根目录的 `npm run dev`、`npm run build` 和 `npm test` 仍指向 React/Fastify 旧链路。不要在剩余真实环境验收完成前切换这些脚本，也不要删除旧 Agent loop、预览审批、源文件哈希检查或 `UseLegacy` 回滚入口。

禁止提交 `.env`、数据库、上传内容、配对码、设备凭据、短期模型令牌或私有签名密钥。
