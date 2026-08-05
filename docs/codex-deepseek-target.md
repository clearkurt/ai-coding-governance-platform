# Codex + DeepSeek 目标架构

状态：目标链路主体已实现并通过本地自动化验收，但尚未切换生产流量。更新日期：2026-08-04。

## 架构

```text
Vue 3 Web ── HTTP/SSE ── FastAPI ── PostgreSQL
                              │
                     validated WSS
                              │
                       Windows Rust daemon
                              │ stdio JSON-RPC
                              ▼
                    pinned Codex App Server
                              │ short-lived task token
                              ▼
                 company Responses proxy ── DeepSeek
```

中央服务负责身份、团队/项目归属、任务、审批、审计、配额及 Responses 代理。真实 DeepSeek API key 只存在中央服务。Rust 守护进程负责配对、Credential Manager、WSS、授权目录、清洗后的影子工作区、同步前哈希条件、备份/回滚、Codex 生命周期和审计转发。App Server 不暴露网络端口。

## 已实施状态

- `apps/web-next`：Vue 登录、设备/项目发现、会话与任务、SSE 续传、审批、取消和回滚界面。
- `apps/api-next`：FastAPI 会话、配对、设备 WSS、幂等任务投递、事件去重、SSE、审批/回滚、审计、短期任务令牌、DeepSeek Responses 透明代理和配额控制。
- `apps/agent`：Codex stdio JSON-RPC、任务映射/中断、进程树终止、影子工作区、同步冲突保护、备份/回滚、敏感信息脱敏，以及公司管理目录中的运行时安装与启动校验。
- 运行时契约固定为 daemon 构建支持的 Codex、App Server schema、模型目录和配置模板版本；不搜索 `PATH`。

这些是实现状态，不代表真实生产环境已经验收。当前自动化证据和未完成门槛以 [rollout-acceptance.md](rollout-acceptance.md) 为准。

## 安全边界

1. 命令在 Codex workspace sandbox 与审批策略内执行；守护进程负责超时和完整进程树终止。
2. 工作区命令默认无网络；模型流量只进入公司 Responses 代理，真实上游密钥不下发设备。
3. 任务只在 daemon 管理的影子工作区运行；同步回原项目前检查原文件哈希并保留任务级备份。
4. 首期不支持 MCU 编译器、烧录器、串口或 JTAG/SWD 等硬件能力。
5. 项目根目录由用户选择；本地绝对路径不进入中央数据库，拒绝链接/重解析点越界。
6. 凭据、密钥和令牌不得进入模型、日志或审计；上传前进行路径和敏感内容脱敏。
7. push、发布、部署、Webhook 等外部副作用必须明确批准或禁止。
8. 审批、模型元数据、文件/命令事件、取消、失败和回滚形成关联审计；备份失败时不得写回。

更多细节见 [影子工作区安全](shadow-workspace-security.md) 和 [运行时固定安全](runtime-pinning-security.md)。

## 迁移规则

- Vue/FastAPI/PostgreSQL 与旧 React/Fastify/SQLite 并行运行。
- 根 package scripts 暂时保持旧链路；`UseLegacy` 保留。
- 只有完成真实 PostgreSQL、DeepSeek、固定 Codex artifact、生产 TLS/WSS、灰度与故障注入验收后，才可讨论切换默认入口。
- 只有新链路安全控制被证明等价且回滚演练通过后，才可删除旧 Agent loop 和旧工具。
