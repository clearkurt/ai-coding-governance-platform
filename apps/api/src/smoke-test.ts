import { resolve } from 'node:path';

declare const WebSocket: { new (url: string): { addEventListener(type: string, handler: (event: any) => void): void; removeEventListener(type: string, handler: (event: any) => void): void; send(data: string): void; close(): void; }; };

process.env.NODE_ENV = 'test';
process.env.DATA_DIR = resolve(process.cwd(), '../../data', `smoke-${Date.now()}`);
const { createUser } = await import('./auth.js');
const { app } = await import('./server.js');
const { runServerAgent } = await import('./agent-runner.js');
const { AgentTaskBridge } = await import('./agent-task-bridge.js');
const bridge = new AgentTaskBridge();
const bridged = bridge.wait('task-test', 1000);
if (!bridge.complete('task-test', { status:'completed', result:{ok:true} }) || !(await bridged).result) throw new Error('Agent task bridge 未关联结果');
const agentRun = await runServerAgent({
  async generate() { throw new Error('测试不应走 generate'); },
  async plan(input) { return input.toolResults?.length ? { kind:'final', generation:{ analysis:'ok', suggestion:'ok', code:'void ok(void) {}', cautions:[] } } : { kind:'tool_call', toolCall:{ name:'read_file', arguments:{ relativePath:'src/main.c' } } }; }
}, { requirement:'读取工程', codeStyle:'C99', sources:[] }, async call => ({ name:call.name, content:'int main(void) { return 0; }' }));
if (!agentRun.code.includes('void ok')) throw new Error('Agent loop 未完成工具回合');

const unauthorized = await app.inject({ method: 'GET', url: '/api/conversations' });
if (unauthorized.statusCode !== 401) throw new Error('未登录访问未被拒绝');
await createUser('tester', 'safe-password-123');
const login = await app.inject({ method: 'POST', url: '/api/auth/login', payload: { username: 'tester', password: 'safe-password-123' } });
if (login.statusCode !== 200) throw new Error(`登录失败：${login.body}`);
const setCookie = login.headers['set-cookie'];
const cookie = (Array.isArray(setCookie) ? setCookie[0] : setCookie!).split(';')[0];
const pairing = await app.inject({ method: 'POST', url: '/api/agent/pairing-codes', headers: { cookie } });
if (pairing.statusCode !== 200 || !pairing.json().code) throw new Error(`配对码创建失败：${pairing.body}`);
const created = await app.inject({ method: 'POST', url: '/api/conversations', headers: { cookie }, payload: { title: 'LCD 冒号闪烁' } });
if (created.statusCode !== 200) throw new Error(`创建会话失败：${created.body}`);
const conversationId = created.json().conversation.id;
const generated = await app.inject({ method: 'POST', url: `/api/conversations/${conversationId}/messages`, headers: { cookie }, payload: { requirement: '添加 LCD 冒号闪烁', fileIds: [] } });
const generatedBody = generated.json();
if (generated.statusCode !== 200 || !generatedBody.message?.content?.trim()) throw new Error(`生成失败：${generated.body}`);

// WebSocket 端到端回归：task_result 不带 credential 也必须被接受（select_root 修复）
const address = await app.listen({ port: 0, host: '127.0.0.1' });
const ws = new WebSocket(`ws://${address.replace('http://', '')}/api/agent/ws`);
await new Promise<void>((resolve, reject) => { ws.addEventListener('open', () => resolve()); ws.addEventListener('error', () => reject(new Error('WS 连接失败'))); });
const wsMessage = (timeoutMs = 5000) => new Promise<any>((resolve, reject) => { const timer = setTimeout(() => reject(new Error('WS 消息超时')), timeoutMs); const onMessage = (event: any) => { clearTimeout(timer); ws.removeEventListener('message', onMessage); resolve(JSON.parse(String(event.data))); }; ws.addEventListener('message', onMessage); });
ws.send(JSON.stringify({ version:1, messageId:'smoke-pair', type:'pair', payload:{ code:pairing.json().code, name:'smoke-agent', publicKey:'smoke-key', version:'0.1.0', roots:[{ label:'smoke-root' }] } }));
const pairResult = await wsMessage();
if (pairResult.type !== 'pair_result') throw new Error(`WS 配对失败：${JSON.stringify(pairResult)}`);
const deviceId = pairResult.deviceId;
const credential = pairResult.payload.credential;
const rootId = pairResult.payload.roots[0].id;
ws.send(JSON.stringify({ version:1, messageId:'smoke-hello', type:'hello', deviceId, payload:{ credential, version:'0.1.0', rules:[{ rootId, content:'团队规则 ABC' }] } }));
const helloResult = await wsMessage();
if (helloResult.type !== 'hello_result') throw new Error('WS hello 失败');
const devicesAfterHello = await app.inject({ method:'GET', url:'/api/agents', headers:{ cookie } });
const helloRoot = devicesAfterHello.json().devices.find((d:any) => d.id === deviceId)?.roots.find((r:any) => r.id === rootId);
if (!helloRoot || helloRoot.rules !== '团队规则 ABC') throw new Error(`hello 未保存项目规则：${JSON.stringify(helloRoot)}`);
const selectTask = await app.inject({ method:'POST', url:`/api/agents/${deviceId}/tasks`, headers:{ cookie }, payload:{ rootId, kind:'select_root', payload:{} } });
if (selectTask.statusCode !== 200) throw new Error(`select_root 创建失败：${selectTask.body}`);
const taskMessage = await wsMessage();
if (taskMessage.type !== 'task' || taskMessage.taskId !== selectTask.json().taskId) throw new Error('select_root 任务未通过 WS 下发');
ws.send(JSON.stringify({ version:1, messageId:'smoke-result', type:'task_result', deviceId, taskId: taskMessage.taskId, payload:{ status:'completed', result:{ rootId, label:'新目录', rules:'新规则 XYZ' } } }));
const taskDone = await new Promise<any>((resolve, reject) => { const timer = setInterval(async () => { try { const detail = await app.inject({ method:'GET', url:`/api/agent-tasks/${taskMessage.taskId}`, headers:{ cookie } }); const status = detail.json().task?.status; if (['completed','failed','expired','rejected'].includes(status)) { clearInterval(timer); resolve(detail.json().task); } } catch { /* retry */ } }, 50); setTimeout(() => { clearInterval(timer); reject(new Error('select_root 任务状态超时')); }, 5000); });
if (taskDone.status !== 'completed') throw new Error(`select_root 未完成：${JSON.stringify(taskDone)}`);
const devicesAfter = await app.inject({ method:'GET', url:'/api/agents', headers:{ cookie } });
const updatedRoot = devicesAfter.json().devices.find((d:any) => d.id === deviceId)?.roots.find((r:any) => r.id === rootId);
if (!updatedRoot || updatedRoot.label !== '新目录' || updatedRoot.rules !== '新规则 XYZ') throw new Error(`select_root 标签/规则未更新：${JSON.stringify(updatedRoot)}`);

// 补丁审批令牌链路：stage_patch → awaiting_approval → approve → apply_patch 携带令牌
const stageTask = await app.inject({ method:'POST', url:`/api/agents/${deviceId}/tasks`, headers:{ cookie }, payload:{ rootId, kind:'stage_patch', payload:{ relativePath:'src/main.c', originalSha256:'a'.repeat(64), newContent:'int main(void) { return 0; }' } } });
if (stageTask.statusCode !== 200) throw new Error(`stage_patch 创建失败：${stageTask.body}`);
const stageMsg = await wsMessage();
if (stageMsg.type !== 'task' || stageMsg.taskId !== stageTask.json().taskId) throw new Error('stage_patch 未通过 WS 下发');
ws.send(JSON.stringify({ version:1, messageId:'smoke-stage-result', type:'task_result', deviceId, taskId: stageMsg.taskId, payload:{ status:'awaiting_approval', result:{ status:'awaiting_approval', relativePath:'src/main.c', originalSha256:'a'.repeat(64), preview:{ before:'x', after:'y' } } } }));
await new Promise<any>((resolve, reject) => { const timer = setInterval(async () => { try { const detail = await app.inject({ method:'GET', url:`/api/agent-tasks/${stageMsg.taskId}`, headers:{ cookie } }); const status = detail.json().task?.status; if (status === 'awaiting_approval') { clearInterval(timer); resolve(detail.json().task); } else if (['completed','failed','expired','rejected'].includes(status)) { clearInterval(timer); reject(new Error(`stage 任务异常：${status}`)); } } catch { /* retry */ } }, 50); setTimeout(() => { clearInterval(timer); reject(new Error('stage 任务未进入等待审批状态')); }, 5000); });
const approveResult = await app.inject({ method:'POST', url:`/api/agent-tasks/${stageMsg.taskId}/approve`, headers:{ cookie } });
if (approveResult.statusCode !== 200) throw new Error(`审批失败：${approveResult.body}`);
const applyMsg = await wsMessage();
if (applyMsg.type !== 'task' || applyMsg.payload.kind !== 'apply_patch') throw new Error('审批后未下发 apply_patch 任务');
if (!applyMsg.payload.approvalToken || !applyMsg.payload.approvalTokenHash || applyMsg.payload.approvalTokenHash.length !== 64) throw new Error('apply_patch 任务缺少审批令牌');
ws.send(JSON.stringify({ version:1, messageId:'smoke-apply-result', type:'task_result', deviceId, taskId: applyMsg.taskId, payload:{ status:'completed', result:{ relativePath:'src/main.c', sha256:'x' } } }));

// run_command 审批链路：awaiting_approval → approve → 携带令牌重新派发 → completed
const cmdTask = await app.inject({ method:'POST', url:`/api/agents/${deviceId}/tasks`, headers:{ cookie }, payload:{ rootId, kind:'run_command', payload:{ command:'git status', cwd:'' } } });
if (cmdTask.statusCode !== 200) throw new Error(`run_command 创建失败：${cmdTask.body}`);
const cmdMsg = await wsMessage();
if (cmdMsg.type !== 'task' || cmdMsg.taskId !== cmdTask.json().taskId || cmdMsg.payload.kind !== 'run_command') throw new Error('run_command 未通过 WS 下发');
ws.send(JSON.stringify({ version:1, messageId:'smoke-cmd-awaiting', type:'task_result', deviceId, taskId: cmdMsg.taskId, payload:{ status:'awaiting_approval', result:{ status:'awaiting_approval', command:'git status', cwd:'', reason:'需要批准' } } }));
await new Promise<any>((resolve, reject) => { const timer = setInterval(async () => { try { const detail = await app.inject({ method:'GET', url:`/api/agent-tasks/${cmdMsg.taskId}`, headers:{ cookie } }); const status = detail.json().task?.status; if (status === 'awaiting_approval') { clearInterval(timer); resolve(detail.json().task); } else if (['completed','failed','expired','rejected'].includes(status)) { clearInterval(timer); reject(new Error(`命令任务异常：${status}`)); } } catch { /* retry */ } }, 50); setTimeout(() => { clearInterval(timer); reject(new Error('命令任务未进入等待审批状态')); }, 5000); });
const cmdApprove = await app.inject({ method:'POST', url:`/api/agent-tasks/${cmdMsg.taskId}/approve`, headers:{ cookie } });
if (cmdApprove.statusCode !== 200) throw new Error(`命令审批失败：${cmdApprove.body}`);
const cmdExecMsg = await wsMessage();
if (cmdExecMsg.type !== 'task' || cmdExecMsg.payload.kind !== 'run_command') throw new Error('审批后未下发 run_command 任务');
if (!cmdExecMsg.payload.approvalToken || !cmdExecMsg.payload.approvalTokenHash || cmdExecMsg.payload.approvalTokenHash.length !== 64) throw new Error('run_command 任务缺少审批令牌');
if (cmdExecMsg.payload.command !== 'git status') throw new Error('run_command 命令被篡改');
ws.send(JSON.stringify({ version:1, messageId:'smoke-cmd-result', type:'task_result', deviceId, taskId: cmdExecMsg.taskId, payload:{ status:'completed', result:{ status:'completed', command:'git status', cwd:'', exitCode:0, stdout:'', stderr:'', durationMs:5 } } }));
const cmdDone = await new Promise<any>((resolve, reject) => { const timer = setInterval(async () => { try { const detail = await app.inject({ method:'GET', url:`/api/agent-tasks/${cmdExecMsg.taskId}`, headers:{ cookie } }); const status = detail.json().task?.status; if (['completed','failed','rejected','expired'].includes(status)) { clearInterval(timer); resolve(detail.json().task); } } catch { /* retry */ } }, 50); setTimeout(() => { clearInterval(timer); reject(new Error('run_command 完成状态超时')); }, 5000); });
if (cmdDone.status !== 'completed') throw new Error(`run_command 未完成：${JSON.stringify(cmdDone)}`);

// run_command 拒绝链路
const denyTask = await app.inject({ method:'POST', url:`/api/agents/${deviceId}/tasks`, headers:{ cookie }, payload:{ rootId, kind:'run_command', payload:{ command:'npm test', cwd:'' } } });
if (denyTask.statusCode !== 200) throw new Error(`run_command 拒绝链路创建失败：${denyTask.body}`);
const denyMsg = await wsMessage();
if (denyMsg.type !== 'task' || denyMsg.taskId !== denyTask.json().taskId) throw new Error('run_command 拒绝链路未通过 WS 下发');
ws.send(JSON.stringify({ version:1, messageId:'smoke-cmd-deny', type:'task_result', deviceId, taskId: denyMsg.taskId, payload:{ status:'awaiting_approval', result:{ status:'awaiting_approval', command:'npm test', cwd:'', reason:'需要批准' } } }));
await new Promise<void>((resolve, reject) => { const timer = setInterval(async () => { try { const detail = await app.inject({ method:'GET', url:`/api/agent-tasks/${denyMsg.taskId}`, headers:{ cookie } }); if (detail.json().task?.status === 'awaiting_approval') { clearInterval(timer); resolve(); } } catch { /* retry */ } }, 50); setTimeout(() => { clearInterval(timer); reject(new Error('拒绝前状态超时')); }, 5000); });
const denyResult = await app.inject({ method:'POST', url:`/api/agent-tasks/${denyMsg.taskId}/reject`, headers:{ cookie } });
if (denyResult.statusCode !== 200) throw new Error(`命令拒绝失败：${denyResult.body}`);
const denyDetail = await app.inject({ method:'GET', url:`/api/agent-tasks/${denyMsg.taskId}`, headers:{ cookie } });
if (denyDetail.json().task?.status !== 'rejected') throw new Error(`run_command 未按预期拒绝：${denyDetail.body}`);

ws.close();

// 会话绑定项目：按项目创建、列表返回 rootId、禁止中途切换
const boundConv = await app.inject({ method:'POST', url:'/api/conversations', headers:{ cookie }, payload:{ title:'绑定项目会话', rootId } });
if (boundConv.statusCode !== 200 || boundConv.json().conversation.rootId !== rootId) throw new Error(`会话绑定项目失败：${boundConv.body}`);
const boundId = boundConv.json().conversation.id;
const convList = await app.inject({ method:'GET', url:'/api/conversations', headers:{ cookie } });
const boundInList = convList.json().conversations.find((c:any) => c.id === boundId);
if (!boundInList || boundInList.rootId !== rootId) throw new Error('会话列表未返回 rootId');
const switchBlocked = await app.inject({ method:'POST', url:'/api/chat/stream', headers:{ cookie }, payload:{ conversationId: boundId, content:'测试切换', deviceId, rootId:'00000000-0000-0000-0000-000000000000', sources:[] } });
if (switchBlocked.statusCode !== 409) throw new Error(`中途切换项目应被拒绝：${switchBlocked.statusCode} ${switchBlocked.body}`);

console.log('API smoke test passed');
await app.close();
