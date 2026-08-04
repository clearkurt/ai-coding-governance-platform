# 企业内部 AI 编程助手平台

面向嵌入式研发团队的内部 AI 编程助手。当前仓库已经实现“中央 Web/API + Windows 本地 Rust Agent”的对话、设备配对、本地工程访问、命令执行、修改审批和审计链路。

项目已确认下一阶段架构方向：使用本地 Codex App Server 作为通用编程 Agent 内核，使用 DeepSeek 原生 Responses API 作为模型后端，并将现有 Rust Agent 缩减为设备桥接守护进程。该方向目前是设计决策，尚未完成代码迁移；当前运行方式仍以现有实现为准。

## 当前技术栈

- React + Vite + TypeScript 中文前端
- Fastify + TypeScript 服务端
- Node.js 24 内置 SQLite 与 Argon2id 密码哈希，无 Docker、无独立数据库
- Windows Rust 本地 Agent（当前为执行器，目标为 Codex 设备桥接守护进程）
- 规范文档：[knowledge/code-style.md](knowledge/code-style.md)

## 已确认的目标技术栈

- 前端：Vue 3、TypeScript、Vite、Pinia、Vue Router、Naive UI
- 中央 API：Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2（异步）、Alembic、HTTPX
- 数据库：PostgreSQL
- 浏览器通信：普通操作使用 HTTP，任务与对话流使用 SSE
- 设备通信：中央 API 与 Windows 守护进程之间使用 WSS
- 本地控制：Rust 守护进程通过 stdio JSON-RPC 控制 Codex App Server
- Agent 与模型：Codex App Server + DeepSeek Responses API
- 测试：Vitest、Playwright、pytest、cargo test

目标栈以新需求为准，不要求复用当前 React、Fastify 或 SQLite 实现。迁移完成前，当前栈仍用于运行现有系统。

## 已确认的目标架构

```text
浏览器 Web UI
      │ HTTP/SSE
中央 API ── 用户、设备、项目、审计、配额、DeepSeek Responses 代理
      │ WSS 反向连接
Windows 本地守护进程
      │ stdio JSON-RPC
Codex App Server ── 本地项目文件与命令
```

关键决策：

- Codex App Server 负责 Agent loop、线程、工具调用、文件编辑、Shell 和审批协议。
- DeepSeek 原生支持 Codex 所需的 Responses API；当前选定并已人工验证 `deepseek-v4-flash`。
- DeepSeek 主 API Key 只保存在中央服务。客户端使用设备绑定的短期令牌访问公司的 Responses 透明代理。
- 本地守护进程继续使用主动 WSS 连接中央 API；Codex App Server 不直接暴露远程端口。
- 守护进程通过 stdio JSON-RPC 启停和控制固定版本的 Codex App Server，并转发 thread/turn/item 事件。
- Rust 守护进程保留设备配对、Credential Manager、心跳重连、目录选择、运行时更新、备份回滚和审计上报；不再长期维护第二套通用 Agent 工具系统。
- Codex 运行时、模型目录、配置模板和 JSON-RPC schema 由产品固定并灰度升级，不使用用户 `PATH` 中不受控的 Codex 版本。

完整决策及迁移边界见 [docs/codex-deepseek-target.md](docs/codex-deepseek-target.md)。

## 快速启动（开发机）

要求 Node.js 24 或更高版本。

```powershell
npm install
Copy-Item .env.example .env
$env:ADMIN_USERNAME = 'admin'
$env:ADMIN_PASSWORD = '请设置强密码'
npm run init-admin
npm run dev
```

浏览器访问 `http://localhost:5173`。开发时前端自动转发 `/api` 到 `http://localhost:3000`。

## Windows Server 部署

```powershell
npm ci
npm run build
$env:NODE_ENV = 'production'
$env:HOST = '0.0.0.0'
$env:PORT = '3000'
$env:DATA_DIR = 'D:\ai-coding-platform-data'
$env:SESSION_SECRET = '替换为长随机字符串'
$env:ADMIN_USERNAME = 'admin'
$env:ADMIN_PASSWORD = '替换为强密码'
npm run init-admin
npm start
```

生产模式由 Fastify 同时提供 API 和网页静态文件，访问 `http://服务器地址:3000`。Windows 防火墙只需为内网开放该端口。`DATA_DIR` 中的 `platform.sqlite` 与 `uploads` 是业务数据，应定期备份；恢复时停止服务后还原整个目录。

## 当前接口

- `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`
- `POST /api/files`：最多 5 个 `.c`、`.h`、`.txt`、`.md`，每个最大 1 MB
- `POST /api/conversations`、`GET /api/conversations`、`GET /api/conversations/:id`
- `POST /api/conversations/:id/messages`

管理员账号仅通过 `init-admin` 初始化。请勿提交 `.env`、数据库、上传文件或真实模型密钥。
## 当前实现说明

当前平台支持“服务端 LLM + Windows 本地 Agent”部署方式：LLM 运行在公司内网模型网关，网页选择设备和授权工程根目录后，服务端可通过 Agent 工具调用读取文件、生成补丁，并在用户确认后写入本地工程。

复制 `.env.example` 后配置：

```powershell
$env:LLM_BASE_URL = 'https://llm-gateway.example.internal/v1'
$env:LLM_API_KEY = '公司模型网关密钥'
$env:LLM_MODEL = 'company-coder'
```

未配置模型网关时，测试环境使用 Mock provider；生产环境必须配置受控的公司模型网关。当前实现仍由自研服务端 Agent loop 和 Rust Agent 执行 `list_files`、`read_file`、`stage_patch`、`apply_patch`、`run_command`、`select_root`。这些说明用于迁移期间识别现状，不代表目标架构。

详细需求和架构见 [docs/requirements.md](docs/requirements.md)、[docs/architecture.md](docs/architecture.md) 与 [docs/codex-deepseek-target.md](docs/codex-deepseek-target.md)。
