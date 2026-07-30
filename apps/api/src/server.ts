import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, extname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import Fastify, { type FastifyReply, type FastifyRequest } from 'fastify';
import cookie from '@fastify/cookie';
import cors from '@fastify/cors';
import multipart from '@fastify/multipart';
import statics from '@fastify/static';
import { z } from 'zod';
import { config } from './config.js';
import { audit, db, id, now } from './db.js';
import { createUser, getUser, login, logout, readSession, signSession } from './auth.js';
import { MockModelProvider } from './model.js';

const allowedExtensions = new Set(['.c', '.h', '.txt', '.md']);
const provider = new MockModelProvider();
const app = Fastify({ logger: true });
await app.register(cookie);
await app.register(cors, { origin: true, credentials: true });
await app.register(multipart, { limits: { files: 5, fileSize: 1024 * 1024 } });
mkdirSync(config.uploadDir, { recursive: true });

function userOf(request: FastifyRequest) { return getUser(readSession(request.cookies.session)); }
function requireUser(request: FastifyRequest, reply: FastifyReply) { const user = userOf(request); if (!user) { reply.code(401).send({ error: '请先登录' }); return null; } return user; }
function styleVersion() { const git = spawnSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).stdout.trim(); const content = readFileSync(config.codeStylePath, 'utf8'); return git || createHash('sha256').update(content).digest('hex'); }
function renderGeneration(g: Awaited<ReturnType<typeof provider.generate>>) { return `## 分析\n${g.analysis}\n\n## 建议\n${g.suggestion}\n\n## 代码草案\n\`\`\`c\n${g.code}\`\`\`\n\n## 注意事项\n${g.cautions.map(x => `- ${x}`).join('\n')}`; }

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

if (existsSync(config.webDistPath)) { await app.register(statics, { root: config.webDistPath, wildcard: false }); app.get('/*', async (_request, reply) => reply.sendFile('index.html')); }
if (process.env.NODE_ENV !== 'test') app.listen({ port: config.port, host: config.host });
export { app };
