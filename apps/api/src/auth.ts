import { randomBytes, timingSafeEqual, createHmac } from 'node:crypto';
import { db, id, now, audit } from './db.js';
import { config } from './config.js';

const cryptoWithArgon = await import('node:crypto') as unknown as typeof import('node:crypto') & { argon2: Function };
const ARGON_PARAMS = { parallelism: 1, tagLength: 32, memory: 19456, passes: 2 };
function derive(password: string, salt: Buffer): Promise<Buffer> {
  return new Promise((resolve, reject) => cryptoWithArgon.argon2('argon2id', { message: Buffer.from(password), nonce: salt, ...ARGON_PARAMS }, (error: Error | null, value: Buffer) => error ? reject(error) : resolve(value)));
}
export async function hashPassword(password: string) { const salt = randomBytes(16); return `${salt.toString('base64')}:${(await derive(password, salt)).toString('base64')}`; }
export async function verifyPassword(password: string, stored: string) { const [saltText, hashText] = stored.split(':'); const actual = await derive(password, Buffer.from(saltText, 'base64')); const expected = Buffer.from(hashText, 'base64'); return actual.length === expected.length && timingSafeEqual(actual, expected); }
export async function createUser(username: string, password: string) { db.prepare('INSERT INTO users (id,username,password_hash,created_at) VALUES (?,?,?,?)').run(id(), username, await hashPassword(password), now()); }
export async function login(username: string, password: string) { const user = db.prepare('SELECT * FROM users WHERE username=?').get(username) as { id:string; username:string; password_hash:string } | undefined; if (!user || !(await verifyPassword(password, user.password_hash))) return null; const sessionId = id(); db.prepare('INSERT INTO sessions (id,user_id,expires_at) VALUES (?,?,?)').run(sessionId, user.id, new Date(Date.now()+7*864e5).toISOString()); audit(user.id, 'login', { username }); return { id: sessionId, user: { id: user.id, username: user.username } }; }
export function getUser(sessionId?: string) { if (!sessionId) return null; return db.prepare('SELECT u.id,u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires_at>?').get(sessionId, now()) as {id:string;username:string}|undefined; }
export function logout(sessionId?: string) { if (sessionId) db.prepare('DELETE FROM sessions WHERE id=?').run(sessionId); }
export function signSession(sessionId: string) { return `${sessionId}.${createHmac('sha256', config.sessionSecret).update(sessionId).digest('base64url')}`; }
export function readSession(value?: string) { if (!value) return undefined; const [sessionId, signature] = value.split('.'); return signature && timingSafeEqual(Buffer.from(signature), Buffer.from(createHmac('sha256', config.sessionSecret).update(sessionId).digest('base64url'))) ? sessionId : undefined; }
