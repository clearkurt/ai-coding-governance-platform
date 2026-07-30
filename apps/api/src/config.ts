import 'dotenv/config';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const dataPath = process.env.DATA_DIR ? resolve(process.env.DATA_DIR) : resolve(projectRoot, 'data');

export const config = {
  port: Number(process.env.PORT ?? 3000),
  host: process.env.HOST ?? '0.0.0.0',
  dataDir: dataPath,
  sessionSecret: process.env.SESSION_SECRET ?? 'development-only-change-me',
  uploadDir: resolve(dataPath, 'uploads'),
  databasePath: resolve(dataPath, 'platform.sqlite'),
  codeStylePath: resolve(projectRoot, 'knowledge', 'code-style.md'),
  webDistPath: resolve(projectRoot, 'apps', 'web', 'dist')
};
