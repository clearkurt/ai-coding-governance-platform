import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { config } from './config.js';

mkdirSync(dirname(config.databasePath), { recursive: true });
export const db = new DatabaseSync(config.databasePath);
db.exec(`
  PRAGMA foreign_keys = ON;
  CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), role TEXT NOT NULL, content TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), original_name TEXT NOT NULL, stored_name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS audit_logs (id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL, public_key TEXT NOT NULL, credential_hash TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'offline', last_seen_at TEXT, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS device_roots (id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id), label TEXT NOT NULL, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS pairing_codes (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), code_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_tasks (id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id), user_id TEXT NOT NULL REFERENCES users(id), root_id TEXT NOT NULL REFERENCES device_roots(id), kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, approval_token_hash TEXT, expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(id), status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS agent_releases (id TEXT PRIMARY KEY, version TEXT NOT NULL UNIQUE, download_url TEXT NOT NULL, sha256 TEXT NOT NULL, signature TEXT NOT NULL, min_protocol INTEGER NOT NULL, published_at TEXT NOT NULL);
`);
const conversationColumns = db.prepare('PRAGMA table_info(conversations)').all() as Array<{ name: string }>;
if (!conversationColumns.some(column => column.name === 'root_id')) {
  db.exec('ALTER TABLE conversations ADD COLUMN root_id TEXT');
}
const rootColumns = db.prepare('PRAGMA table_info(device_roots)').all() as Array<{ name: string }>;
if (!rootColumns.some(column => column.name === 'rules')) {
  db.exec('ALTER TABLE device_roots ADD COLUMN rules TEXT');
}

export const id = () => crypto.randomUUID();
export const now = () => new Date().toISOString();
export function audit(userId: string | null, action: string, detail: unknown) {
  db.prepare('INSERT INTO audit_logs (id,user_id,action,detail,created_at) VALUES (?,?,?,?,?)').run(id(), userId, action, JSON.stringify(detail), now());
}
