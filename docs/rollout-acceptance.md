# 灰度与验收

状态：本地自动化验收通过；生产环境验收未完成，因此旧路径和默认生产入口必须保留。

## 已通过的验收

截至 2026-08-04，本次迁移分支已通过：

| 范围 | 结果 |
|---|---|
| FastAPI | 普通 `pytest` 43 项通过、5 项真实 PostgreSQL integration 默认 skipped；migration、`PostgresStore` lifecycle、同进程 localhost WebSocket reconnect、uvicorn 子进程重启与 custom-format backup/restore integration 均已在本机 PostgreSQL 16 显式运行通过；`ruff check .` 通过 |
| Vue | Vitest 2 项通过；生产构建通过；Playwright 1 项通过 |
| Rust daemon | `cargo test` 51 项通过，2 项真实 artifact smoke 默认 ignored；初始化与 localhost Responses turn smoke 均已在本机显式运行通过 |
| 真实 Codex artifact smoke | 固定版本 `0.145.0-alpha.27` 已通过托管安装、SHA-256 校验、strict config、App Server initialize/initialized 与正常关闭；未触发模型请求 |
| 本地 Responses 协议闭环 | 同一真实 Codex 已通过带认证的固定单模型 catalog、临时 command auth 和 localhost stub 完成固定文本 thread/turn；无审批且 workspace 未变化 |
| 旧链路 | 根目录 legacy build 与 smoke test 通过 |
| 依赖审计 | `npm audit` 0 个漏洞 |

这些结果证明当前代码的自动化契约，不等同于生产就绪。

Responses 流代理已用本地故障流验证：首块后 timeout/read error、下游首次迭代前断开、响应体从未启动、部分消费任务取消，都会幂等关闭上游并精确释放并发槽；只有解析到完整 `response.completed` 才记录一次用量。建立上游连接或非流响应读取阶段的 timeout/HTTP 错误仍映射为 504/502；流式响应 headers 已发送后的中途错误只能中止流，不能再伪装成 HTTP 504。

Daemon 的未确认 `task.event` 现会在本地受限、原子更新的 durable outbox 中持久化；WSS 重连按稳定 key 顺序重发，只有中央 ACK 成功后才持久删除。daemon 或 App Server 在活动任务期间异常退出时，会在没有既存 terminal 事件的前提下持久化确定性的 `turn/failed` 恢复事件，避免中央任务永久停在 running；已落盘但 ACK 丢失的 terminal 事件优先，不会被 synthetic failure 覆盖。该保证覆盖进程退出、App Server 崩溃和连接发送失败；临时文件会 `sync_all`，但当前未对父目录执行目录级同步，且 Windows 文件替换语义有限，因此不宣称突然断电后的目录元数据一定持久。

## 尚未验证的生产门槛

- 本机 PostgreSQL 16 已完成真实 Alembic upgrade/downgrade/re-upgrade：upgrade 后共 19 张表（含 `alembic_version`）、21 个外键、26 个唯一约束，`task_status` 6 个值；downgrade 后仅余版本表且 enum 为 0；再次 upgrade 成功。真实 `PostgresStore` lifecycle 会在任务提交、未 ACK 审批决定和未 ACK 回滚之间关闭旧 session，并用全新 `AsyncSession/PostgresStore` 验证相同 delivery 的恢复及 ACK 后消失。另有独立 integration 验证完整 FastAPI ASGI 进程退出、连接注册表丢失及新进程对未确认 delivery 的恢复。custom-format backup/restore 演练也已在本机显式通过：恢复到全新数据库后验证 18 张业务表、21 个外键、26 个唯一约束、完整枚举，以及 pending task/approval、事件、模型用量和审计数据。生产同版本实例的备份、恢复和故障演练仍未完成。
- 环境变量控制的 localhost socket integration 已用真实 uvicorn 和 `websockets` client 通过，验证了 dispatch ACK 前断线后的相同 delivery 重放、event ACK 前断线后的 source ID 去重与相同 sequence，以及 approval/rollback delivery 重放和 ACK。它只验证本机明文 `ws://127.0.0.1`，不证明 TLS、反向代理、证书链或生产 WSS。
- uvicorn 子进程重启 integration 已在本机 PostgreSQL 16 显式运行通过：第一个真实 ASGI 进程在 dispatch ACK 前退出，第二个全新进程和连接注册表连接同一随机 PostgreSQL 数据库，并重放完全相同的 task/delivery 后成功 ACK。它只证明 localhost 进程边界恢复，不代表生产进程管理、TLS 或多实例协调。
- 使用真实 DeepSeek key 验证 Responses API 流式转发、错误映射、限额和令牌过期/撤销。
- 使用公司批准且固定 SHA-256 的真实 Codex artifact 完成 Windows 端到端 DeepSeek 模型任务、审批、取消、同步和回滚。真实 artifact 安装、初始化及 localhost Responses stub 文本 turn 已通过，但 stub 不证明 DeepSeek 行为、模型质量或完整企业任务闭环。
- 验证生产证书、域名、反向代理和 WSS TLS 链，包括断线重连与证书失败行为。
- 完成小规模设备灰度、故障注入和长任务测试，包括进程崩溃、网络抖动、服务重启、磁盘不足、并发冲突和审计重放。
- Durable outbox 仍是单机本地文件，不替代磁盘损坏、磁盘耗尽、设备丢失或跨设备复制测试；达到条数/单事件/总量上限时 daemon 会拒绝继续假装成功，需要运维介入并保留旧链路。

任一项未完成时，不得删除旧路径、移除 `UseLegacy` 或把根 package scripts/生产流量切到目标链路。

## 逐步灰度

1. 在隔离环境用生产同版本 PostgreSQL 重跑已通过的自动化迁移与备份恢复演练，保留输出并再次校验数据约束和审计完整性。
2. 用专用低权限 DeepSeek key 和受控项目完成 Responses 流式与失败场景验收。
3. 将公司发布记录中的固定 Codex artifact 安装到测试 Windows 设备，验证 manifest、hash、版本和完整任务闭环。
4. 在生产同构 TLS/WSS 环境运行故障注入与长任务；确认重连、幂等、配额、取消和回滚。
5. 仅为内部测试团队开启新链路，观察错误率、任务成功率、审批丢失、同步冲突、审计缺口和回滚成功率。
6. 分批扩大设备比例；每批必须经过观察窗口并保留旧入口。
7. 全量稳定且回滚演练通过后，另行评审默认入口切换；删除旧路径属于后续独立变更。

## 回滚条件

出现以下任一情况立即停止扩量并切回旧链路：认证或项目隔离失效；凭据/源码泄露风险；审计事件缺失或乱序不可恢复；任务重复执行；审批绕过；同步覆盖用户新修改；备份或回滚失败；Codex pin 校验失败；持续 WSS/Responses 错误；错误率或长任务失败率超过发布时批准的阈值。

回滚时保留数据库和审计证据，停止新链路任务投递，等待在途任务进入可判定状态，并使用 `UseLegacy`/旧入口恢复服务。具体数值阈值、观察窗口和负责人必须在生产变更单中批准，不能由本文件预设。
