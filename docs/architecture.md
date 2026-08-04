# LLM Agent 架构

> 状态说明：下方第一部分记录当前实现；第二部分是已经确认但尚未完成迁移的目标架构。新开发不得把目标架构误写成已经上线。

## 当前实现

```text
网页 UI
  │ 需求 / 确认 diff
  ▼
API 编排层 ── 身份、权限、任务、审计、模型网关
  │                         ▲
  │ tool request             │ tool result
  ▼                         │
本地 Agent ── list_files / read_file / stage_patch / apply_patch
  │
  ▼
用户明确授权的本地工程根目录
```

LLM 运行在 API 编排层，但拥有通过 Agent 工具访问本地工程的能力。它可以读取文件并发起写入工具调用；Agent 仍强制执行授权根目录、路径校验、文件限制、哈希校验和企业写入策略。Agent 不负责推理，只负责安全工具执行、心跳和结果回传。

模型 provider 必须实现统一接口，以便切换公司内网模型、OpenAI-compatible 网关和测试 Mock，而不改变会话、审计和 Agent 协议。

## 已确认的目标架构

```text
网页 UI
  │ HTTP/SSE
  ▼
中央 API（FastAPI）── 认证、设备/项目、任务、审计、配额、Responses 透明代理
  │ WSS（本地主动反连）
  ▼
Windows 设备桥接守护进程
  │ stdio JSON-RPC
  ▼
固定版本 Codex App Server ── 文件、Shell、线程、工具与审批
  │
  ▼
用户授权的本地工程目录
```

目标架构使用 Codex App Server 代替自研通用 Agent loop 和 Rust 通用执行工具。DeepSeek 通过原生 Responses API 为 Codex 提供模型能力。中央 API 不转换 Chat Completions 协议，只透明代理 Responses 流量、验证短期设备令牌、注入服务端 DeepSeek 密钥并记录用量。

本地守护进程不是第二个 Coding Agent。它只负责设备生命周期、中央通信、Codex 子进程管理、stdio JSON-RPC 适配、事件转发、目录授权、备份回滚和审计上报。Codex App Server 不得直接暴露到远程网络。

完整决策、保留能力和迁移顺序见 [codex-deepseek-target.md](codex-deepseek-target.md)。

目标应用栈已经确定为 Vue 3 + FastAPI + PostgreSQL。浏览器使用 HTTP 提交操作、SSE 接收任务事件；FastAPI 与本地守护进程通过 WSS 双向通信。该选择不受当前 React、Fastify、SQLite 实现约束。
