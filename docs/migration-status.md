# 迁移状态

新架构正在 `apps/web-next` 与 `apps/api-next` 中并行建设；旧 React、Fastify、SQLite 和 Rust 执行器仍是当前可用链路，尚未切换或删除。

## 阶段一至三

- [x] 记录旧系统构建与测试基线。
- [x] 建立 Vue 3 + TypeScript + Vite + Pinia + Vue Router + Naive UI 前端骨架。
- [x] 建立 FastAPI + PostgreSQL + SQLAlchemy async + Alembic 后端骨架。
- [x] 建立团队隔离、任务事件序号和幂等约束的核心模型。
- [x] 建立用户会话认证、设备 WSS 鉴权、任务下发和浏览器 SSE 事件续传纵向链路。
- [x] 接入固定版本 Codex App Server 本地 stdio JSON-RPC 桥接、WSS 任务映射与进程树治理。
- [ ] 接入 DeepSeek Responses 透明代理、短期模型令牌与审批闭环。

`apps/web-next` 使用 5174 端口，目标 FastAPI 使用 8081 端口，避免与旧系统默认开发端口冲突。启动说明分别位于各新应用目录的 README；数据库迁移前必须提供 PostgreSQL，不能使用 SQLite 替代。
