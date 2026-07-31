import { createHash, randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, extname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import Fastify, { type FastifyReply, type FastifyRequest } from 'fastify';
import cookie from '@fastify/cookie';
import cors from '@fastify/cors';
import multipart from '@fastify/multipart';
import statics from '@fastify/static';
import websocket from '@fastify/websocket';
import { z } from 'zod';
import { config } from './config.js';
import { audit, db, id, now } from './db.js';
import { createUser, getUser, login, logout, readSession, signSession } from './auth.js';
import { MockModelProvider, OpenAICompatibleProvider, validateAgentToolCall, type ModelProvider } from './model.js';
import { AgentTaskBridge } from './agent-task-bridge.js';
import { runServerAgent } from './agent-runner.js';

const allowedExtensions = new Set(['.c', '.h', '.txt', '.md']);
const provider: ModelProvider = config.llmBaseUrl && config.llmApiKey && process.env.NODE_ENV !== 'test'
  ? new OpenAICompatibleProvider(config.llmBaseUrl, config.llmApiKey, config.llmModel)
  : new MockModelProvider();
const app = Fastify({ logger: true });
await app.register(cookie);
await app.register(cors, { origin: true, credentials: true });
await app.register(multipart, { limits: { files: 5, fileSize: 1024 * 1024 } });
await app.register(websocket);
mkdirSync(config.uploadDir, { recursive: true });

function userOf(request: FastifyRequest) { return getUser(readSession(request.cookies.session)); }
function requireUser(request: FastifyRequest, reply: FastifyReply) { const user = userOf(request); if (!user) { reply.code(401).send({ error: '请先登录' }); return null; } return user; }
function styleVersion() { const git = spawnSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).stdout.trim(); const content = readFileSync(config.codeStylePath, 'utf8'); return git || createHash('sha256').update(content).digest('hex'); }
function renderGeneration(g: Awaited<ReturnType<typeof provider.generate>>) { return `## 分析\n${g.analysis}\n\n## 建议\n${g.suggestion}\n\n## 代码草案\n\`\`\`c\n${g.code}\`\`\`\n\n## 注意事项\n${g.cautions.map(x => `- ${x}`).join('\n')}`; }
const agentSockets = new Map<string, { send: (message: string) => void }>();
const agentTaskBridge = new AgentTaskBridge();
const sha = (value: string) => createHash('sha256').update(value).digest('hex');
const taskEvent = (taskId: string, status: string, detail: unknown) => db.prepare('INSERT INTO agent_task_events (id,task_id,status,detail,created_at) VALUES (?,?,?,?,?)').run(id(), taskId, status, JSON.stringify(detail), now());
function sendPendingTasks(deviceId: string) { const current = now(); db.prepare("SELECT id FROM agent_tasks WHERE device_id=? AND status IN ('queued','dispatched','approved','awaiting_approval') AND expires_at<=?").all(deviceId, current).forEach((task: any) => { db.prepare("UPDATE agent_tasks SET status='expired',updated_at=? WHERE id=? AND status NOT IN ('completed','failed','rejected','expired')").run(current, task.id); taskEvent(String(task.id), 'expired', {}); }); const socket = agentSockets.get(deviceId); if (!socket) return; const tasks = db.prepare("SELECT * FROM agent_tasks WHERE device_id=? AND status IN ('queued','approved') AND expires_at>? ORDER BY created_at").all(deviceId, current) as Array<Record<string, unknown>>; for (const task of tasks) { db.prepare("UPDATE agent_tasks SET status='dispatched',updated_at=? WHERE id=? AND status IN ('queued','approved')").run(current,String(task.id)); taskEvent(String(task.id),'dispatched',{}); socket.send(JSON.stringify({ version: 1, messageId: id(), type: 'task', deviceId, taskId: task.id, payload: JSON.parse(String(task.payload)) })); } }
function dispatchAgentTask(userId:string, deviceId:string, rootId:string, kind:string, payload:Record<string, unknown>) {
  const taskId=id(); const expiresAt=new Date(Date.now()+10*60_000).toISOString();
  db.prepare('INSERT INTO agent_tasks (id,device_id,user_id,root_id,kind,payload,status,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)').run(taskId,deviceId,userId,rootId,kind,JSON.stringify({...payload,rootId,kind}),'queued',expiresAt,now(),now());
  taskEvent(taskId,'queued',{}); audit(userId,'agent_task_created',{taskId,deviceId,kind});
  const result=agentTaskBridge.wait(taskId);
  sendPendingTasks(deviceId);
  return { taskId, expiresAt, result };
}

app.post('/api/auth/login', async (request, reply) => {
  const body = z.object({ username: z.string().min(1).max(64), password: z.string().min(1).max(200) }).safeParse(request.body);
  if (!body.success) return reply.code(400).send({ error: '用户名或密码格式错误' });
  const result = await login(body.data.username, body.data.password);
  if (!result) return reply.code(401).send({ error: '用户名或密码错误' });
  reply.setCookie('session', signSession(result.id), { httpOnly: true, sameSite: 'lax', path: '/', secure: process.env.NODE_ENV === 'production', maxAge: 7 * 86400 });
  return { user: result.user };
});
app.post('/api/auth/logout', async (request, reply) => { logout(readSession(request.cookies.session)); reply.clearCookie('session', { path: '/' }); return { ok: true }; });
app.get('/api/auth/me', async (request) => ({ user: userOf(request) ?? null }));

app.post('/api/files', async (request, reply) => {
  const user = requireUser(request, reply); if (!user) return;
  const stored = [] as Array<{id:string;name:string;size:number}>;
  try {
    for await (const part of request.files()) {
      const name = basename(part.filename || 'unnamed'); const extension = extname(name).toLowerCase();
      if (!allowedExtensions.has(extension)) { part.file.resume(); return reply.code(400).send({ error: '仅允许 .c、.h、.txt、.md 文件' }); }
      const data = await part.toBuffer(); if (part.file.truncated) return reply.code(400).send({ error: '单文件不能超过 1 MB' });
      const fileId = id(); const storedName = `${fileId}${extension}`; writeFileSync(join(config.uploadDir, storedName), data);
      db.prepare('INSERT INTO uploads (id,user_id,original_name,stored_name,size,sha256,created_at) VALUES (?,?,?,?,?,?,?)').run(fileId, user.id, name, storedName, data.length, createHash('sha256').update(data).digest('hex'), now());
      audit(user.id, 'file_upload', { fileId, name, size: data.length }); stored.push({ id: fileId, name, size: data.length });
    }
  } catch { return reply.code(400).send({ error: '文件上传失败：最多 5 个文件，单个不超过 1 MB' }); }
  return { files: stored };
});

app.post('/api/conversations', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const title = z.object({title:z.string().min(1).max(100)}).safeParse(request.body); if (!title.success) return reply.code(400).send({error:'标题不能为空'}); const conversation = { id:id(), title:title.data.title, createdAt:now() }; db.prepare('INSERT INTO conversations (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)').run(conversation.id,user.id,conversation.title,conversation.createdAt,conversation.createdAt); return { conversation }; });
app.get('/api/conversations', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; return { conversations: db.prepare('SELECT id,title,created_at AS createdAt,updated_at AS updatedAt FROM conversations WHERE user_id=? ORDER BY updated_at DESC').all(user.id) }; });
app.get('/api/conversations/:id', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const conversation = db.prepare('SELECT id,title,created_at AS createdAt FROM conversations WHERE id=? AND user_id=?').get((request.params as {id:string}).id,user.id); if (!conversation) return reply.code(404).send({error:'会话不存在'}); const messages = db.prepare('SELECT id,role,content,metadata,created_at AS createdAt FROM messages WHERE conversation_id=? ORDER BY created_at').all((conversation as {id:string}).id); return { conversation, messages }; });
app.post('/api/conversations/:id/messages', async (request, reply) => {
  const user = requireUser(request, reply); if (!user) return; const conversationId = (request.params as {id:string}).id;
  if (!db.prepare('SELECT 1 FROM conversations WHERE id=? AND user_id=?').get(conversationId,user.id)) return reply.code(404).send({error:'会话不存在'});
  const body = z.object({ requirement:z.string().min(1).max(10000), fileIds:z.array(z.string().uuid()).max(5).default([]) }).safeParse(request.body); if (!body.success) return reply.code(400).send({error:'需求或文件参数无效'});
  const sourceFiles = body.data.fileIds.map(fileId => db.prepare('SELECT original_name,stored_name FROM uploads WHERE id=? AND user_id=?').get(fileId,user.id) as {original_name:string;stored_name:string}|undefined);
  if (sourceFiles.some(x => !x)) return reply.code(400).send({error:'包含无权限的文件'});
  const sources = sourceFiles.filter(Boolean).map(file => ({ name:file!.original_name, content:readFileSync(join(config.uploadDir,file!.stored_name),'utf8') }));
  const codeStyle = readFileSync(config.codeStylePath,'utf8'); const version = styleVersion(); audit(user.id,'generation_requested',{conversationId,fileIds:body.data.fileIds,styleVersion:version});
  db.prepare('INSERT INTO messages (id,conversation_id,role,content,created_at) VALUES (?,?,?,?,?)').run(id(),conversationId,'user',body.data.requirement,now());
  try { const result = await provider.generate({ requirement:body.data.requirement, codeStyle, sources }); const content = renderGeneration(result); const message={id:id(),role:'assistant',content,createdAt:now()}; db.prepare('INSERT INTO messages (id,conversation_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)').run(message.id,conversationId,message.role,content,JSON.stringify({styleVersion}),message.createdAt); db.prepare('UPDATE conversations SET updated_at=? WHERE id=?').run(message.createdAt,conversationId); audit(user.id,'generation_completed',{conversationId,styleVersion:version}); return { message, generation:result }; } catch (error) { audit(user.id,'generation_failed',{conversationId,reason:error instanceof Error ? error.message : 'unknown'}); return reply.code(500).send({error:'生成失败'}); }
});

app.post('/api/agent-runs', async (request, reply) => {
  const user = requireUser(request, reply); if (!user) return;
  const body = z.object({ requirement:z.string().min(1).max(10000), deviceId:z.string().uuid(), rootId:z.string().uuid(), conversationId:z.string().uuid().optional(), sources:z.array(z.object({name:z.string(),content:z.string()})).max(20).default([]) }).safeParse(request.body);
  if (!body.success) return reply.code(400).send({error:'Agent run 参数无效'});
  if (!db.prepare('SELECT 1 FROM devices WHERE id=? AND user_id=?').get(body.data.deviceId,user.id)) return reply.code(404).send({error:'设备不存在'});
  if (!db.prepare('SELECT 1 FROM device_roots WHERE id=? AND device_id=?').get(body.data.rootId,body.data.deviceId)) return reply.code(400).send({error:'项目根目录无效'});
  const agentProvider = provider as Partial<import('./model.js').AgentModelProvider>;
  if (typeof agentProvider.plan !== 'function') return reply.code(503).send({error:'当前模型不支持 Agent 工具调用'});
  try {
    const codeStyle=readFileSync(config.codeStylePath,'utf8');
    const toolTrace:Array<{name:string;result:unknown}> = [];
    const generation=await runServerAgent(agentProvider as import('./model.js').AgentModelProvider,{requirement:body.data.requirement,codeStyle,sources:body.data.sources},async call=>{
      if (!['list_files','read_file','stage_patch','apply_patch'].includes(call.name)) throw new Error('不支持的 Agent 工具');
      const safeCall = validateAgentToolCall(call);
      const task=dispatchAgentTask(user.id,body.data.deviceId,body.data.rootId,safeCall.name,safeCall.arguments);
      return { taskId:task.taskId, ...(await task.result) };
    }, 8, (call,result) => toolTrace.push({name:call.name,result}));
    if (body.data.conversationId && db.prepare('SELECT 1 FROM conversations WHERE id=? AND user_id=?').get(body.data.conversationId, user.id)) {
      const userMessageId = id(); const assistantMessageId = id(); const content = renderGeneration(generation);
      db.prepare('INSERT OR IGNORE INTO messages (id,conversation_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)').run(userMessageId, body.data.conversationId, 'user', body.data.requirement, JSON.stringify({ agent: true }), now());
      db.prepare('INSERT INTO messages (id,conversation_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)').run(assistantMessageId, body.data.conversationId, 'assistant', content, JSON.stringify({ agent: true, toolTrace }), now());
      db.prepare('UPDATE conversations SET updated_at=? WHERE id=?').run(now(), body.data.conversationId);
    }
    audit(user.id,'agent_run_completed',{deviceId:body.data.deviceId,rootId:body.data.rootId});
    return { generation, toolTrace };
  } catch (error) { audit(user.id,'agent_run_failed',{reason:error instanceof Error ? error.message : 'unknown'}); return reply.code(500).send({error:'Agent run 失败'}); }
});

app.post('/api/agent-runs/stream', async (request, reply) => {
  const user = requireUser(request, reply); if (!user) return;
  const body = z.object({ requirement:z.string().min(1).max(10000), deviceId:z.string().uuid(), rootId:z.string().uuid(), conversationId:z.string().uuid().optional(), sources:z.array(z.object({name:z.string(),content:z.string()})).max(20).default([]) }).safeParse(request.body);
  if (!body.success) return reply.code(400).send({ error: 'Agent run 参数无效' });
  if (!db.prepare('SELECT 1 FROM devices WHERE id=? AND user_id=?').get(body.data.deviceId,user.id)) return reply.code(404).send({ error: '设备不存在' });
  if (!db.prepare('SELECT 1 FROM device_roots WHERE id=? AND device_id=?').get(body.data.rootId,body.data.deviceId)) return reply.code(400).send({ error: '项目根目录无效' });
  const agentProvider = provider as Partial<import('./model.js').AgentModelProvider>;
  if (typeof agentProvider.plan !== 'function') return reply.code(503).send({ error: '当前模型不支持 Agent 工具调用' });
  reply.hijack(); reply.raw.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'keep-alive' });
  const send = (event: string, data: unknown) => reply.raw.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  try {
    send('start', { conversationId: body.data.conversationId });
    const codeStyle = readFileSync(config.codeStylePath,'utf8'); const toolTrace:Array<{name:string;result:unknown}> = [];
    const generation = await runServerAgent(agentProvider as import('./model.js').AgentModelProvider, { requirement:body.data.requirement, codeStyle, sources:body.data.sources }, async call => { const safeCall = validateAgentToolCall(call); const task = dispatchAgentTask(user.id, body.data.deviceId, body.data.rootId, safeCall.name, safeCall.arguments); const result = { taskId: task.taskId, ...(await task.result) }; return result; }, 8, (call,result) => { toolTrace.push({ name:call.name, result }); send('tool', { name:call.name, result }); }, delta => send('token', { text: delta }));
    if (body.data.conversationId && db.prepare('SELECT 1 FROM conversations WHERE id=? AND user_id=?').get(body.data.conversationId,user.id)) { const created = now(); db.prepare('INSERT OR IGNORE INTO messages (id,conversation_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)').run(id(),body.data.conversationId,'user',body.data.requirement,JSON.stringify({agent:true}),created); db.prepare('INSERT INTO messages (id,conversation_id,role,content,metadata,created_at) VALUES (?,?,?,?,?,?)').run(id(),body.data.conversationId,'assistant',renderGeneration(generation),JSON.stringify({agent:true,toolTrace}),now()); db.prepare('UPDATE conversations SET updated_at=? WHERE id=?').run(now(),body.data.conversationId); }
    send('complete', { generation, toolTrace }); audit(user.id,'agent_run_completed',{deviceId:body.data.deviceId,rootId:body.data.rootId});
  } catch (error) { send('error', { error: error instanceof Error ? error.message : 'Agent run failed' }); audit(user.id,'agent_run_failed',{reason:error instanceof Error ? error.message : 'unknown'}); } finally { send('done', {}); reply.raw.end(); }
});

app.post('/api/agent/pairing-codes', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const code = randomBytes(9).toString('base64url'); const expiresAt = new Date(Date.now() + 10 * 60_000).toISOString(); db.prepare('INSERT INTO pairing_codes (id,user_id,code_hash,expires_at,created_at) VALUES (?,?,?,?,?)').run(id(), user.id, sha(code), expiresAt, now()); audit(user.id, 'agent_pairing_code_created', { expiresAt }); return { code, expiresAt }; });
app.get('/api/agents', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const devices = db.prepare('SELECT id,name,version,status,last_seen_at AS lastSeenAt,created_at AS createdAt FROM devices WHERE user_id=? ORDER BY created_at DESC').all(user.id) as Array<Record<string, unknown>>; return { devices: devices.map(device => ({ ...device, roots: db.prepare('SELECT id,label FROM device_roots WHERE device_id=?').all(String(device.id)) })) }; });
app.get('/api/agents/:id', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const device = db.prepare('SELECT id,name,version,status,last_seen_at AS lastSeenAt,created_at AS createdAt FROM devices WHERE id=? AND user_id=?').get((request.params as {id:string}).id, user.id); if (!device) return reply.code(404).send({ error: '设备不存在' }); return { device, roots: db.prepare('SELECT id,label FROM device_roots WHERE device_id=?').all((device as {id:string}).id) }; });
app.post('/api/agents/:id/tasks', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const deviceId = (request.params as {id:string}).id; if (!db.prepare('SELECT 1 FROM devices WHERE id=? AND user_id=?').get(deviceId,user.id)) return reply.code(404).send({error:'设备不存在'}); const parsed = z.object({ rootId:z.string().uuid(), kind:z.enum(['list_files','read_file','stage_patch','select_root']), payload:z.record(z.unknown()) }).safeParse(request.body); if (!parsed.success) return reply.code(400).send({error:'任务参数无效'}); if (!db.prepare('SELECT 1 FROM device_roots WHERE id=? AND device_id=?').get(parsed.data.rootId,deviceId)) return reply.code(400).send({error:'项目根目录无效'}); if (parsed.data.kind==='select_root' && db.prepare("SELECT 1 FROM agent_tasks WHERE device_id=? AND root_id=? AND kind='select_root' AND status IN ('queued','dispatched','running','awaiting_approval')").get(deviceId,parsed.data.rootId)) return reply.code(409).send({error:'该目录已有更换任务正在等待 Agent 处理'}); const taskId=id(); const expiresAt=new Date(Date.now()+10*60_000).toISOString(); db.prepare('INSERT INTO agent_tasks (id,device_id,user_id,root_id,kind,payload,status,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)').run(taskId,deviceId,user.id,parsed.data.rootId,parsed.data.kind,JSON.stringify({...parsed.data.payload,rootId:parsed.data.rootId,kind:parsed.data.kind}),'queued',expiresAt,now(),now()); taskEvent(taskId,'queued',{}); audit(user.id,'agent_task_created',{taskId,deviceId,kind:parsed.data.kind}); sendPendingTasks(deviceId); return { taskId, status:'queued', expiresAt }; });
app.get('/api/agent-tasks/:id', async (request, reply) => { const user=requireUser(request,reply); if (!user) return; const task=db.prepare('SELECT id,device_id AS deviceId,root_id AS rootId,kind,status,payload,expires_at AS expiresAt,created_at AS createdAt,updated_at AS updatedAt FROM agent_tasks WHERE id=? AND user_id=?').get((request.params as {id:string}).id,user.id) as Record<string,unknown>|undefined; if (!task) return reply.code(404).send({error:'任务不存在'}); const events=db.prepare('SELECT status,detail,created_at AS createdAt FROM agent_task_events WHERE task_id=? ORDER BY created_at DESC').all(String(task.id)); return {task:{...task,payload:JSON.parse(String(task.payload))},events:events.map((event:any)=>({...event,detail:JSON.parse(event.detail)}))}; });
app.post('/api/agent-tasks/:id/approve', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const taskId=(request.params as {id:string}).id; const task=db.prepare("SELECT * FROM agent_tasks WHERE id=? AND user_id=? AND kind='stage_patch' AND status='awaiting_approval' AND expires_at>?").get(taskId,user.id) as Record<string,unknown>|undefined; if (!task) return reply.code(409).send({error:'没有可批准的补丁任务'}); const token=randomBytes(24).toString('base64url'); const payload={...JSON.parse(String(task.payload)), approvalToken:token, kind:'apply_patch'}; db.prepare("UPDATE agent_tasks SET status='approved',approval_token_hash=?,payload=?,updated_at=? WHERE id=?").run(sha(token),JSON.stringify(payload),now(),taskId); taskEvent(taskId,'approved',{}); audit(user.id,'agent_patch_approved',{taskId}); sendPendingTasks(String(task.device_id)); return { taskId,status:'approved' }; });
app.post('/api/agent-tasks/:id/reject', async (request, reply) => { const user = requireUser(request, reply); if (!user) return; const taskId = (request.params as { id: string }).id; const task = db.prepare("SELECT * FROM agent_tasks WHERE id=? AND user_id=? AND kind='stage_patch' AND status='awaiting_approval' AND expires_at>?").get(taskId, user.id, now()) as Record<string, unknown> | undefined; if (!task) return reply.code(409).send({ error: '没有可拒绝的补丁任务' }); db.prepare("UPDATE agent_tasks SET status='rejected',updated_at=? WHERE id=?").run(now(), taskId); taskEvent(taskId, 'rejected', {}); audit(user.id, 'agent_patch_rejected', { taskId }); agentTaskBridge.complete(taskId, { status: 'rejected' }); return { taskId, status: 'rejected' }; });
app.get('/api/agent-releases/latest', async (_request, reply) => { const release=db.prepare('SELECT version,download_url AS downloadUrl,sha256,signature,min_protocol AS minProtocol,published_at AS publishedAt FROM agent_releases ORDER BY published_at DESC LIMIT 1').get(); return release ? { release } : reply.code(404).send({error:'暂无发布版本'}); });

app.get('/api/agent/ws', { websocket: true }, (socket) => {
  let deviceId: string | undefined;
  socket.on('message', (raw: Buffer) => { try { const message=JSON.parse(raw.toString()) as {type:string;payload?:Record<string,unknown>;taskId?:string;deviceId?:string};
    if (message.type==='pair') { const p=message.payload ?? {}; const code=String(p.code ?? ''); const pairing=db.prepare('SELECT * FROM pairing_codes WHERE code_hash=? AND used_at IS NULL AND expires_at>?').get(sha(code),now()) as Record<string,unknown>|undefined; if (!pairing) return socket.send(JSON.stringify({type:'pair_error',payload:{error:'配对码无效或已过期'}})); const newDeviceId=id(); const credential=randomBytes(32).toString('base64url'); db.prepare('INSERT INTO devices (id,user_id,name,public_key,credential_hash,version,status,last_seen_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)').run(newDeviceId,String(pairing.user_id),String(p.name ?? 'Windows Agent'),String(p.publicKey ?? ''),sha(credential),String(p.version ?? '0.1.0'),'online',now(),now()); const roots=[] as Array<{id:string;label:string}>; for (const root of Array.isArray(p.roots)?p.roots:[]) { const label=String((root as Record<string,unknown>).label ?? '项目目录'); const rootId=id(); db.prepare('INSERT INTO device_roots (id,device_id,label,created_at) VALUES (?,?,?,?)').run(rootId,newDeviceId,label,now()); roots.push({id:rootId,label}); } db.prepare('UPDATE pairing_codes SET used_at=? WHERE id=?').run(now(),String(pairing.id)); audit(String(pairing.user_id),'agent_paired',{deviceId:newDeviceId}); socket.send(JSON.stringify({version:1,messageId:id(),type:'pair_result',deviceId:newDeviceId,payload:{deviceId:newDeviceId,credential,roots}})); return; }
    const p=message.payload ?? {}; const requestedDevice=String(message.deviceId ?? ''); const credential=String(p.credential ?? ''); const device=db.prepare('SELECT * FROM devices WHERE id=? AND credential_hash=?').get(requestedDevice,sha(credential)) as Record<string,unknown>|undefined; if (!device) return socket.close(); deviceId=requestedDevice; agentSockets.set(deviceId,socket); db.prepare("UPDATE devices SET status='online',last_seen_at=?,version=? WHERE id=?").run(now(),String(p.version ?? device.version),deviceId); if (message.type==='hello') { socket.send(JSON.stringify({version:1,messageId:id(),type:'hello_result',deviceId,payload:{protocol:1}})); sendPendingTasks(deviceId); return; }
    if (message.type==='heartbeat') { db.prepare("UPDATE devices SET status='online',last_seen_at=? WHERE id=?").run(now(),deviceId); return; }
    if (message.type==='task_status' || message.type==='task_result' || message.type==='task_error') { const taskId=String(message.taskId ?? ''); const task=db.prepare('SELECT * FROM agent_tasks WHERE id=? AND device_id=?').get(taskId,deviceId) as Record<string,unknown>|undefined; if (!task) return; const status=String(p.status ?? (message.type==='task_result'?'completed':message.type==='task_error'?'failed':'running')); if (String(task.kind)==='select_root' && status==='completed') { const result=(p.result ?? {}) as Record<string,unknown>; const label=String(result.label ?? '项目目录'); db.prepare('UPDATE device_roots SET label=? WHERE id=? AND device_id=?').run(label,String(task.root_id),deviceId); } db.prepare('UPDATE agent_tasks SET status=?,updated_at=? WHERE id=?').run(status,now(),taskId); taskEvent(taskId,status,p); audit(String(task.user_id),'agent_task_event',{taskId,status}); if (['completed','failed','rejected','expired','awaiting_approval'].includes(status)) agentTaskBridge.complete(taskId,{status,result:p.result,error:typeof p.error==='string'?p.error:undefined}); }
  } catch { socket.send(JSON.stringify({type:'error',payload:{error:'invalid_message'}})); } });
  socket.on('close', () => { if (deviceId && agentSockets.get(deviceId) === socket) { agentSockets.delete(deviceId); db.prepare("UPDATE devices SET status='offline' WHERE id=?").run(deviceId); } });
});

if (existsSync(config.webDistPath)) { await app.register(statics, { root: config.webDistPath, wildcard: false }); app.get('/*', async (_request, reply) => reply.sendFile('index.html')); }
if (process.env.NODE_ENV !== 'test') app.listen({ port: config.port, host: config.host });
export { app };
