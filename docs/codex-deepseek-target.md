# Codex + DeepSeek 目标架构

状态：已确认，正在并行迁移
更新日期：2026-08-04

## 架构决策

平台不再长期维护第二套通用 Coding Agent 运行时。目标是由每台 Windows 设备上的固定版本 Codex App Server 承担 Agent loop、工具、文件编辑、Shell 与审批协议；中央平台继续承担企业身份、设备和项目归属、任务持久化、审计、配额与恢复能力。

`deepseek-v4-flash` 已由项目负责人验证可用于预期工作流。它通过 DeepSeek 原生 Responses API 接入，不建设 Chat Completions 转换层。

```text
Vue 3 Web UI
    │ HTTP / SSE
    ▼
FastAPI 中央服务 ── PostgreSQL
    ├─ 身份、设备、项目、任务、审批、审计和配额
    └─ DeepSeek Responses 透明代理（持有真实 API Key）
    │ validated WSS
    ▼
Windows Rust 守护进程
    ├─ 配对、Credential Manager、心跳、重连和授权目录
    ├─ 固定 Codex 运行时、任务备份、回滚与审计转发
    └─ stdio JSON-RPC
         ▼
    Codex App Server
         └─ 用户明确授权的本地工程
```

Codex App Server 不开放网络端口。中央服务只能通过守护进程已有的 WSS 反向通道访问设备。

## 技术栈

| 层级 | 选型 |
|---|---|
| Web | Vue 3、TypeScript、Vite、Pinia、Vue Router、Naive UI |
| 中央 API | Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2 async、Alembic、HTTPX |
| 数据库 | PostgreSQL |
| 浏览器事件 | SSE；命令和审批使用普通 HTTP |
| 设备通道 | 经过身份校验的 WSS |
| 本地控制 | Rust 守护进程与 Codex App Server 间的 stdio JSON-RPC |
| 模型 | DeepSeek `deepseek-v4-flash` 原生 Responses API |
| 测试 | Vitest、Playwright、pytest、cargo test |

React、Fastify 和 SQLite 属于迁移期间保留的旧实现，不应进入新的目标架构代码。

## 模型认证

- 真实 DeepSeek API Key 只保存在中央服务，禁止进入客户端配置、日志、审计或仓库。
- Codex 自定义 provider 的 `base_url` 指向公司 Responses 代理，而不是 DeepSeek 公网地址。
- 守护进程通过设备凭据为当前任务换取短期、设备绑定、任务绑定、可吊销的模型令牌。
- Codex 使用自定义 provider 的 command auth 调用当前固定安装的 `company-agent model-token`。命令只向标准输出写令牌。
- 第一版每台设备只运行一个活动 Codex 任务，防止 task-bound 令牌在并发任务间串用。
- 中央代理校验令牌、注入上游密钥、流式转发 SSE、执行并发限制与每日团队配额，并按上游请求 ID 幂等记录用量。

## Rust 守护进程职责

- 保留配对、Credential Manager、WSS、心跳、重连、授权目录选择、运行时更新与审计转发。
- 通过绝对路径启动产品携带或安装的固定 Codex 版本；不查找用户 `PATH`，不接受 WindowsApps 中的任意版本。
- 管理 Codex 生命周期、stdio JSON-RPC、thread/turn 映射、事件重放、审批响应和进程树终止。
- 在任务写入前建立有大小和文件数上限的本地备份，并支持经过用户授权的任务级回滚。
- 迁移验收前保留 `UseLegacy` 回退入口。验收后退役自研通用 Agent loop 及 `list_files`、`read_file`、`stage_patch`、`apply_patch`、通用 `run_command` 分类器。

## 八类安全边界

1. **命令执行**：由 Codex workspace sandbox 与审批策略控制。项目不再维护命令名白名单；超时、输出和进程树仍由守护进程约束。
2. **网络访问**：Codex 模型流量只能进入公司 Responses 代理；真实上游密钥只在中央服务。工作区命令默认无网络，确需联网时走明确审批和审计。
3. **文件写入**：只允许用户选定的根目录；启用 workspace-write、任务级备份、哈希/冲突保护和回滚。根目录外默认不可写。
4. **硬件操作**：首期不支持 MCU 编译器、烧录器、串口、JTAG/SWD、擦除、熔丝或 Option Bytes。以后如需支持，使用职责单一的厂商工具接口另行设计。
5. **项目与路径**：项目根目录必须由用户选择；服务端只保存稳定 root ID，不保存本地绝对路径。切换项目由用户触发，并隔离旧项目上下文；拒绝符号链接或重解析点逃逸。
6. **敏感信息**：设备凭据保存在 Windows Credential Manager；`.env`、私钥、凭据目录和令牌不得进入模型、日志或审计。命令输出需要脱敏。
7. **Git 与外部副作用**：本地可逆操作可按策略执行；push、发布、部署、Webhook、邮件和影响他人的动作必须明确批准或禁止。
8. **恢复与审计**：任务修改前必须成功备份；审批、模型请求元数据、文件/命令事件、取消、失败和回滚形成可关联审计记录。备份失败时不得继续写入。

原生 Windows Codex sandbox 当前不承担 MCU 厂商编译器和烧录能力，这两项不属于首期需求。

## 运行时与升级

- 固定 Codex CLI/App Server 版本、配置模板、模型目录和 JSON-RPC schema。
- “运行时自行更新”包括从 `PATH` 使用用户全局安装版本、安装 `latest` 或未经产品验证的替代版本；这些行为都不允许。
- 升级顺序：契约测试 → 真实任务回归 → 少量设备灰度 → 全量发布。
- App Server 只通过本地 stdio 使用；Rust 守护进程仍负责与中央服务通信，因此不能删除本地守护进程。

## 迁移顺序与完成条件

1. 在旧生产链路旁建立 Vue/FastAPI/PostgreSQL 基础设施。
2. 完成中央身份、设备 WSS、任务持久化和浏览器 SSE。
3. 接入固定 Codex App Server，完成 thread/turn、事件、审批、取消和进程恢复。
4. 接入短期模型令牌和 DeepSeek Responses 代理。
5. 完成任务级备份、回滚、审计和配额验证。
6. 双轨运行并验证回退开关；完成真实 PostgreSQL、真实 DeepSeek 与 Windows 端到端测试。
7. 只有在安全控制等价且验收通过后，才删除旧 Agent loop 和通用执行工具。

迁移期间不能因为新路径“可以运行”就提前删除旧安全审批、源文件哈希检查或回退能力。
