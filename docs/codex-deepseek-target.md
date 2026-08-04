# Codex + DeepSeek 目标架构决策

状态：**已确认，尚未实施**
日期：2026-08-04

## 决策摘要

平台不再计划长期维护一套自研通用 Coding Agent 运行时。目标是将本地 Codex App Server 作为通用 Agent 内核，将 DeepSeek 原生 Responses API 作为模型后端，并保留中央 Web/API 与轻量 Windows 设备守护进程作为产品控制面。

`deepseek-v4-flash` 在 Codex Agent loop 中的实际使用效果已经由项目负责人验证，可作为首个接入模型。模型质量不再是本次架构选择的阻塞项；Codex 或模型升级时仍需执行固定冒烟回归。

## 目标拓扑

```text
浏览器 Web UI
      │ HTTP/SSE
      ▼
中央 API
  ├─ 登录与团队用户
  ├─ 设备、项目和任务
  ├─ 审计、配额和用量
  ├─ 短期设备令牌
  └─ DeepSeek Responses 透明代理（持有真实 API Key）
      │ WSS，本地设备主动反连
      ▼
Windows 本地守护进程
  ├─ 配对、Credential Manager、心跳和重连
  ├─ 授权目录选择
  ├─ 固定版本 Codex 运行时管理
  ├─ 任务备份和回滚
  ├─ 事件与脱敏审计转发
  └─ stdio JSON-RPC 客户端
      │
      ▼
Codex App Server
  ├─ thread / turn / item
  ├─ Agent loop 与上下文
  ├─ 文件编辑和 Shell
  ├─ 工具、Skills 与 MCP
  └─ 沙箱和审批协议
      │
      ▼
用户授权的本地工程
```

## 已确认的技术栈

本技术栈按新需求重新选择，不以复用现有实现为约束：

| 层级 | 技术选择 | 主要职责 |
|---|---|---|
| Web 前端 | Vue 3、TypeScript、Vite | 对话、任务、设备、项目、审批和审计界面 |
| 前端状态与路由 | Pinia、Vue Router | Agent 事件状态、会话状态和页面路由 |
| UI | Naive UI | 紧凑的中文 AI 工具界面；允许按产品视觉定制 |
| 中央 API | Python 3.12+、FastAPI | 用户、设备、任务、策略、审计和模型代理 |
| 数据模型 | Pydantic v2 | HTTP、WSS、SSE 和设备事件协议校验 |
| 数据访问 | SQLAlchemy 2 异步、Alembic、asyncpg | PostgreSQL 持久化和迁移 |
| 数据库 | PostgreSQL | 多用户、设备、项目、任务、审批、审计和用量 |
| 上游 HTTP | HTTPX | DeepSeek Responses API 的异步 SSE 透明代理 |
| 浏览器流式通道 | SSE | 文本、命令、文件变化、审批状态和任务完成事件 |
| 设备通道 | WSS | 任务派发、取消、事件回传、审批结果和心跳 |
| 本地守护进程 | Rust | 设备桥接、凭据、Codex 进程、备份、更新和审计 |
| 本地控制协议 | stdio JSON-RPC | 守护进程与 Codex App Server 通信 |
| Agent 内核 | 固定版本 Codex App Server | Agent loop、线程、工具、文件、Shell 和审批 |
| 模型 | DeepSeek `deepseek-v4-flash` | 通过原生 Responses API 提供推理能力 |
| 测试 | Vitest、Playwright、pytest、cargo test | 前端、端到端、API 和守护进程验证 |

首期不引入 Kubernetes、Kafka、微服务拆分、向量数据库或集中源码存储。中央服务可以单体部署；需要多实例设备路由时再评估 Redis 或 NATS。

## 模型与认证

- DeepSeek 已经原生支持 Codex 使用的 Responses API，因此不建设 Responses 到 Chat Completions 转换器。
- 首期模型使用 `deepseek-v4-flash`。
- 真实 DeepSeek API Key 只存中央服务，禁止写入客户端 `.env`、Codex 配置、日志或审计记录。
- Codex 的自定义 provider `base_url` 指向公司中央 Responses 代理，而不是直接指向 DeepSeek。
- 本地守护进程使用现有设备凭据换取短期、设备绑定、可撤销的模型令牌，并通过 Codex 自定义 provider 的命令认证能力提供给 Codex。
- 中央代理负责验证令牌、注入 DeepSeek 凭据、透明转发 SSE、限流、配额和用量记录。

## 本地守护进程的职责

继续保留 Rust 实现及已有的设备基础设施，但改变其定位：

- 保留设备配对、Credential Manager、WSS 反向连接、心跳、断线重连、目录选择、日志和更新器。
- 新增 Codex App Server 子进程管理、stdio JSON-RPC 客户端、thread/turn 映射、事件转发和进程健康检查。
- 保留任务级备份、回滚和企业审计上报能力。
- 不把 Codex App Server 直接暴露为远程 WebSocket 服务；中央服务始终通过守护进程现有 WSS 通道访问设备。
- 迁移完成后退役自研通用工具执行层，包括 `list_files`、`read_file`、`stage_patch`、`apply_patch`、通用 `run_command` 分类器及对应的自研 Agent loop。

本地守护进程可以继续沿用 `company-agent` 名称，但设计和文档中应把它视为 device bridge/daemon，而不是模型工具执行器。

## Codex 运行时管理

- 产品必须携带或安装经过验证的固定 Codex 版本，并通过绝对路径启动。
- 禁止直接调用用户 `PATH` 中的任意 `codex`，避免命中用户自行安装或升级的版本。
- 固定并版本化 Codex 运行时、`models.json`、`config.toml` 模板及 App Server JSON-RPC schema。
- “运行时自行更新”包括用户执行全局 npm 升级、安装 `latest`、或任何未经过产品验证的 Codex 版本替换。产品不得依赖此类版本。
- 升级顺序为：契约测试、真实任务回归、少量设备灰度、全量发布。

## 产品边界

- 首期不要求 Codex 执行 MCU 厂商编译器，也不要求通过 Codex 沙箱解决烧录控制。
- 如未来需要自动编译，应新增边界清晰的厂商构建工具，而不是恢复任意命令白名单系统。
- 保留中央登录、团队与设备归属、项目授权、任务持久化、审计、配额和 Web UI；这些是产品控制面，不由 Codex SDK 替代。
- Codex 负责通用编码 Agent 能力；公司平台负责企业设备、身份、凭据、数据留痕和恢复能力。

## 迁移原则

1. 先建立最小 Codex App Server + DeepSeek 本地验证，不修改现有生产链路。
2. 使用 stdio JSON-RPC 完成线程创建、回合执行、流式事件和中断闭环。
3. 将 Codex 事件映射到现有 WSS/SSE 和 Web UI。
4. 接入中央 Responses 透明代理和短期设备令牌。
5. 增加任务级备份、恢复和审计映射。
6. 与现有链路并行运行并保留回退开关。
7. 验证完成后再删除自研 Agent loop 和通用执行工具。

## 非目标

- 当前不修改任何实现代码或运行时配置。
- 当前不删除现有安全审批、补丁令牌或 Rust 沙箱逻辑。
- 当前不切换生产模型流量。
- 当前不直接把 Codex App Server 暴露给中央服务器或公网。
