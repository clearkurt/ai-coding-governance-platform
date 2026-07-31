# 企业内部 AI 编程助手管控平台

面向嵌入式研发团队的内部 AI 编程助手 MVP。当前版本验证“登录 → 上传源码 → 输入需求 → 按公司规范生成代码草案 → 审计留痕”的闭环；默认使用 Mock 模型，不会执行、编译或改写用户工程文件。

## 技术栈

- React + Vite + TypeScript 中文前端
- Fastify + TypeScript 服务端
- Node.js 24 内置 SQLite 与 Argon2id 密码哈希，无 Docker、无独立数据库
- 规范文档：[knowledge/code-style.md](knowledge/code-style.md)

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
# LLM Agent 重构说明

当前平台支持“服务端 LLM + Windows 本地 Agent”部署方式：LLM 运行在公司内网模型网关，网页选择设备和授权工程根目录后，服务端可通过 Agent 工具调用读取文件、生成补丁，并在用户确认后写入本地工程。

复制 `.env.example` 后配置：

```powershell
$env:LLM_BASE_URL = 'https://llm-gateway.example.internal/v1'
$env:LLM_API_KEY = '公司模型网关密钥'
$env:LLM_MODEL = 'company-coder'
```

未配置模型网关时，测试环境使用 Mock provider；生产环境必须配置受控的公司模型网关。Agent 不执行 Shell、编译器或任意进程，只通过 `list_files`、`read_file`、`stage_patch`、`apply_patch` 工具访问授权工程。

详细需求和架构见 [docs/requirements.md](docs/requirements.md) 与 [docs/architecture.md](docs/architecture.md)。
