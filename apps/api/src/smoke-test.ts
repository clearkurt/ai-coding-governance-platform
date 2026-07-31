import { resolve } from 'node:path';

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
console.log('API smoke test passed');
await app.close();
