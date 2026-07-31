# LLM Agent 架构

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
