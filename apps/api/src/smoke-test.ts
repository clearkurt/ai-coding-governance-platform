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
ws.send(JSON.stringify({ version:1, messageId:'smoke-hello', type:'hello', deviceId, payload:{ credential, version:'0.1.0' } }));
const helloResult = await wsMessage();
if (helloResult.type !== 'hello_result') throw new Error('WS hello 失败');
const selectTask = await app.inject({ method:'POST', url:`/api/agents/${deviceId}/tasks`, headers:{ cookie }, payload:{ rootId, kind:'select_root', payload:{} } });
if (selectTask.statusCode !== 200) throw new Error(`select_root 创建失败：${selectTask.body}`);
const taskMessage = await wsMessage();
if (taskMessage.type !== 'task' || taskMessage.taskId !== selectTask.json().taskId) throw new Error('select_root 任务未通过 WS 下发');
ws.send(JSON.stringify({ version:1, messageId:'smoke-result', type:'task_result', deviceId, taskId: taskMessage.taskId, payload:{ status:'completed', result:{ rootId, label:'新目录' } } }));
const taskDone = await new Promise<any>((resolve, reject) => { const timer = setInterval(async () => { try { const detail = await app.inject({ method:'GET', url:`/api/agent-tasks/${taskMessage.taskId}`, headers:{ cookie } }); const status = detail.json().task?.status; if (['completed','failed','expired','rejected'].includes(status)) { clearInterval(timer); resolve(detail.json().task); } } catch { /* retry */ } }, 50); setTimeout(() => { clearInterval(timer); reject(new Error('select_root 任务状态超时')); }, 5000); });
if (taskDone.status !== 'completed') throw new Error(`select_root 未完成：${JSON.stringify(taskDone)}`);
const devicesAfter = await app.inject({ method:'GET', url:'/api/agents', headers:{ cookie } });
const updatedRoot = devicesAfter.json().devices.find((d:any) => d.id === deviceId)?.roots.find((r:any) => r.id === rootId);
if (!updatedRoot || updatedRoot.label !== '新目录') throw new Error(`select_root 标签未更新：${JSON.stringify(updatedRoot)}`);
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
