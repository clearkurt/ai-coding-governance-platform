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
`);

export const id = () => crypto.randomUUID();
export const now = () => new Date().toISOString();
export function audit(userId: string | null, action: string, detail: unknown) {
  db.prepare('INSERT INTO audit_logs (id,user_id,action,detail,created_at) VALUES (?,?,?,?,?)').run(id(), userId, action, JSON.stringify(detail), now());
}
