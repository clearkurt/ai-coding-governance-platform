# 企业内部 AI 编程助手平台

这是面向内部研发团队的 AI 编程平台。仓库正在从“中央自研 Agent loop + Windows Rust 执行器”迁移为“本地 Codex App Server + DeepSeek Responses API + 企业控制平面”。迁移采用并行目录和回退开关，旧链路在新链路完成安全验收前继续保留。

## 目标技术栈

- Web：Vue 3、TypeScript、Vite、Pinia、Vue Router、Naive UI（`apps/web-next`）
- 中央 API：Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2 async、Alembic、HTTPX（`apps/api-next`）
- 数据库：PostgreSQL
- 浏览器：HTTP + SSE
- 设备：中央 API 与 Windows 守护进程之间使用经过校验的 WSS
- 本地 Agent：Rust 守护进程通过 stdio JSON-RPC 托管固定版本 Codex App Server
- 模型：公司 Responses 代理后的 DeepSeek `deepseek-v4-flash`

旧的 React/Fastify/SQLite 代码位于 `apps/web` 和 `apps/api`，仅用于迁移期运行和回退。不要在新功能中继续扩展旧技术栈。

## 安全原则

- DeepSeek API Key 只存在中央服务。
- 设备凭据保存在 Windows Credential Manager；Codex 只获得短期、设备和任务绑定的模型令牌。
- Codex 运行时、配置模板与协议版本由产品固定，不使用 `PATH` 中任意安装的 Codex。
- 本地路径信任和授权目录校验留在 Rust 守护进程，不移到服务器。
- App Server 不暴露网络端口；中央服务通过守护进程 WSS 转发。
- 新链路具备经过测试的 workspace 边界、任务备份/回滚和等价审计前，不得移除旧安全控制。

完整决策、安全边界和迁移门槛见 [docs/codex-deepseek-target.md](docs/codex-deepseek-target.md)。

## 新链路本地开发

FastAPI：

```powershell
cd apps/api-next
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 8081
pytest
```

Vue：

```powershell
cd apps/web-next
npm install
npm run dev
npm test -- --run
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

Rust 工程固定 GNU toolchain；本机开发时不要改用 MSVC 默认工具链。

## 旧链路

仓库根目录原有的 `npm run dev`、`npm run build` 和 `npm test` 当前仍指向 React/Fastify 旧链路，用于迁移期回归。切换这些命令属于最终割接步骤，不应在新链路尚未验收时提前完成。

禁止提交 `.env`、数据库、上传内容、配对码、设备凭据、短期模型令牌或私有签名密钥。
