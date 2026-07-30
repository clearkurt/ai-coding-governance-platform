import { createUser } from '../auth.js';
const username = process.env.ADMIN_USERNAME; const password = process.env.ADMIN_PASSWORD;
if (!username || !password) throw new Error('请设置 ADMIN_USERNAME 与 ADMIN_PASSWORD 环境变量。');
await createUser(username, password);
console.log(`已创建管理员账号：${username}`);
