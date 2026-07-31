import { config as loadDotenv } from 'dotenv';
import { fileURLToPath } from 'node:url';
import { dirname, isAbsolute, resolve } from 'node:path';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
loadDotenv({ path: resolve(projectRoot, '.env') });
const dataPath = process.env.DATA_DIR ? (isAbsolute(process.env.DATA_DIR) ? process.env.DATA_DIR : resolve(projectRoot, process.env.DATA_DIR)) : resolve(projectRoot, 'data');

export const config = {
  port: Number(process.env.PORT ?? 3000),
  host: process.env.HOST ?? '0.0.0.0',
  dataDir: dataPath,
  sessionSecret: process.env.SESSION_SECRET ?? 'development-only-change-me',
  uploadDir: resolve(dataPath, 'uploads'),
  databasePath: resolve(dataPath, 'platform.sqlite'),
  codeStylePath: resolve(projectRoot, 'knowledge', 'code-style.md'),
  agentPromptPath: resolve(projectRoot, 'knowledge', 'agent-prompt.md'),
  webDistPath: resolve(projectRoot, 'apps', 'web', 'dist'),
  llmBaseUrl: process.env.LLM_BASE_URL ?? '',
  llmApiKey: process.env.LLM_API_KEY ?? '',
  llmModel: process.env.LLM_MODEL ?? 'company-coder'
};
