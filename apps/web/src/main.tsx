import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

type User = { id: string; username: string };
type Conversation = { id: string; title: string; rootId?: string | null };
type Message = { id: string; role: string; content: string };
type Root = { id: string; label: string };
type Device = { id: string; name: string; status: string; version: string; roots: Root[] };
type AgentContext = { deviceId: string; rootId: string };
type Trace = { name: string; result?: any };

const api = async <T,>(url: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(url, { credentials: 'include', ...options, headers: { ...(options?.body instanceof FormData || options?.body === undefined ? {} : { 'Content-Type': 'application/json' }), ...options?.headers } });
  let data: Record<string, unknown> | null = null;
  try { data = await response.json(); } catch { /* 响应体不是 JSON（如服务未启动返回空响应） */ }
  if (!response.ok) {
    const serverError = data && typeof data.error === 'string' ? data.error : undefined;
    throw new Error(serverError ?? (data === null ? `服务暂不可用（HTTP ${response.status}），请稍后重试` : `请求失败（HTTP ${response.status}）`));
  }
  return data as T;
};

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState(''); const [password, setPassword] = useState(''); const [error, setError] = useState('');
  return <main className="login"><section><div className="login-logo">AI</div><p className="eyebrow">EMBEDDED AI WORKBENCH</p><h1>企业 AI 编程助手</h1><p>服务端 LLM Agent + 本地工程工具</p><form onSubmit={async event => { event.preventDefault(); try { const result = await api<{ user: User }>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }); onLogin(result.user); } catch (e) { setError((e as Error).message); } }}><label>用户名<input value={username} onChange={e => setUsername(e.target.value)} required /></label><label>密码<input type="password" value={password} onChange={e => setPassword(e.target.value)} required /></label>{error && <p className="error">{error}</p>}<button>登录</button></form></section></main>;
}

function AgentPanel({ context, onContext, devices, locked }: { context: AgentContext | null; onContext: (context: AgentContext | null) => void; devices: Device[]; locked: boolean }) {
  const [pairCode, setPairCode] = useState(''); const [taskText, setTaskText] = useState('');
  const selected = devices.find(device => device.id === context?.deviceId);
  const wait = async (taskId: string) => { const timer = setInterval(async () => { try { const detail = await api<{ task: { status: string; kind:string }; events: Array<{ status:string; detail?: { error?: string } }> }>(`/api/agent-tasks/${taskId}`); if (['completed', 'failed', 'rejected', 'expired'].includes(detail.task.status)) { clearInterval(timer); if (detail.task.status==='failed') { setTaskText(`任务失败：${detail.events[0]?.detail?.error ?? '未知错误'}`); } else if (detail.task.status==='completed'&&detail.task.kind==='select_root') { setTaskText('目录已更换'); } else if (detail.task.status==='completed') { setTaskText('任务完成'); } else { setTaskText(`任务${detail.task.status}`); } } else { setTaskText(detail.task.status==='dispatched'?'任务已派发，等待 Agent 响应…':`状态：${detail.task.status}`); } } catch { clearInterval(timer); } }, 1000); };
  const task = async (kind: string, payload: Record<string, unknown>) => { if (!context) return; setTaskText('正在打开文件夹选择器，请在弹出的窗口中操作…'); try { const result = await api<{ taskId: string }>(`/api/agents/${context.deviceId}/tasks`, { method: 'POST', body: JSON.stringify({ rootId: context.rootId, kind, payload }) }); wait(result.taskId); } catch (e) { setTaskText((e as Error).message); } };
  return <section className="agent-panel"><div className="agent-title"><div><p className="eyebrow">LOCAL TOOL BRIDGE</p><h3>本地 Agent</h3></div><button onClick={async () => { const result = await api<{ code: string }>('/api/agent/pairing-codes', { method: 'POST' }); setPairCode(result.code); }}>生成配对码</button></div>{pairCode && <div className="pair-code">在 Agent enroll 中输入：<b>{pairCode}</b></div>}<div className="device-list">{devices.length === 0 ? <p>尚未配对设备。</p> : devices.map(device => <button className={device.id === context?.deviceId ? 'device selected-device' : 'device'} key={device.id} disabled={locked} onClick={() => onContext({ deviceId: device.id, rootId: device.roots[0]?.id ?? '' })}><i className={device.status} />{device.name}<small>{device.version} · {device.status}</small></button>)}</div>{selected && <div className="agent-task"><select value={context?.rootId} disabled={locked} onChange={e => onContext({ deviceId: selected.id, rootId: e.target.value })}>{selected.roots.map(root => <option value={root.id} key={root.id}>{root.label}</option>)}</select><button disabled={locked} onClick={() => task('select_root', {})}>更换本地目录</button>{locked && <p className="agent-locked-hint">该对话已绑定项目，切换项目请新建对话</p>}<p className="agent-hint">选中目录后，AI 可自动访问其中的所有文件（.env、私钥等敏感文件除外）</p>{taskText && <pre>{taskText}</pre>}</div>}</section>;
}

const TOOL_ICON: Record<string, string> = { list_files:'📂', read_file:'📄', stage_patch:'✏️', apply_patch:'✅' };
const TOOL_LABEL: Record<string, string> = { list_files:'列出文件', read_file:'读取文件', stage_patch:'生成补丁', apply_patch:'写入补丁' };

function suggestionFromStream(raw: string): string {
  const marker = '"suggestion":';
  const idx = raw.indexOf(marker);
  if (idx < 0) return '';
  let i = idx + marker.length;
  while (i < raw.length && (raw[i] === ' ' || raw[i] === '\t' || raw[i] === '\n' || raw[i] === '\r')) i += 1;
  if (raw[i] !== '"') return '';
  i += 1; let out = '';
  while (i < raw.length) {
    const ch = raw[i];
    if (ch === '\\') {
      if (i + 1 >= raw.length) break;
      const next = raw[i + 1];
      if (next === 'n') out += '\n';
      else if (next === 't') out += '\t';
      else if (next === 'r') out += '\r';
      else if (next === 'u') {
        if (i + 5 >= raw.length) break;
        const code = parseInt(raw.slice(i + 2, i + 6), 16);
        if (!Number.isNaN(code)) out += String.fromCharCode(code);
        i += 5;
      } else out += next;
      i += 2;
      continue;
    }
    if (ch === '"') return out;
    out += ch; i += 1;
  }
  return out;
}

function ToolCard({ trace, onApprove, onReject, status }: { trace: Trace; onApprove: () => void; onReject: () => void; status?: string }) {
  const result = trace.result ?? {};
  const preview = result.preview;
  const file = preview?.relativePath ?? result.relativePath ?? '';
  const isPatch = trace.name === 'stage_patch' && preview;
  const isAwaiting = result.status === 'awaiting_approval';
  const [expanded, setExpanded] = useState(false);
  return <section className="tool-card">
    <div className="tool-header" onClick={() => setExpanded(!expanded)}>
      <span className="tool-icon">{TOOL_ICON[trace.name] ?? '🔧'}</span>
      <strong>{TOOL_LABEL[trace.name] ?? trace.name}</strong>
      {file && <span className="tool-file">{file}</span>}
      <span className={`tool-status tool-status-${status??result.status??'completed'}`}>{status ?? result.status ?? '完成'}</span>
    </div>
    {(expanded || isPatch) && <div className="tool-body">
      {isPatch ? <div className="diff-columns"><div><h4>修改前</h4><pre>{preview.before}</pre></div><div><h4>修改后</h4><pre>{preview.after}</pre></div></div> : <pre className="tool-result">{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>}
      {isPatch && !status && isAwaiting && <div className="diff-actions"><button onClick={onApprove}>确认写入</button><button className="secondary" onClick={onReject}>拒绝</button></div>}
      {status && <p className="task-status">状态：{status}</p>}
    </div>}
  </section>;
}

function App() {
  const [user, setUser] = useState<User | null>(null); const [conversations, setConversations] = useState<Conversation[]>([]); const [active, setActive] = useState<Conversation | null>(null); const [messages, setMessages] = useState<Message[]>([]); const [requirement, setRequirement] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [agentContext, setAgentContext] = useState<AgentContext | null>(null); const [devices, setDevices] = useState<Device[]>([]); const [traces, setTraces] = useState<Trace[]>([]); const [approvalStatus, setApprovalStatus] = useState<Record<string, string>>({}); const [streaming, setStreaming] = useState<Record<string, { id: string; content: string }>>({});
  const activeIdRef = useRef<string | null>(null); activeIdRef.current = active?.id ?? null;
  const loadConversations = async () => { const result = await api<{ conversations: Conversation[] }>('/api/conversations'); setConversations(result.conversations); };
  const del = async (conversation: Conversation) => { if (!confirm(`删除对话“${conversation.title}”？此操作不可恢复。`)) return; try { await api(`/api/conversations/${conversation.id}`, { method: 'DELETE' }); if (active?.id === conversation.id) { setActive(null); setMessages([]); setTraces([]); } loadConversations(); } catch (e) { setError((e as Error).message); } };
  const open = async (conversation: Conversation) => { const result = await api<{ conversation: { rootId?: string | null }; messages: Message[] }>(`/api/conversations/${conversation.id}`); const rootId = result.conversation.rootId ?? null; setActive({ ...conversation, rootId }); setMessages(result.messages); setTraces([]); if (rootId) { for (const device of devices) { const root = device.roots.find(r => r.id === rootId); if (root) { setAgentContext({ deviceId: device.id, rootId }); break; } } } };
  useEffect(() => { api<{ user: User | null }>('/api/auth/me').then(result => { setUser(result.user); if (result.user) loadConversations(); }); }, []);
  useEffect(() => { const load = () => api<{ devices: Device[] }>('/api/agents').then(result => { setDevices(result.devices); setAgentContext(current => current ?? (result.devices[0]?.roots[0] ? { deviceId: result.devices[0].id, rootId: result.devices[0].roots[0].id } : null)); }).catch(() => undefined); load(); const timer = setInterval(load, 5000); return () => clearInterval(timer); }, []);
  if (!user) return <Login onLogin={next => { setUser(next); loadConversations(); }} />;
  const poll = (taskId: string) => { const timer = setInterval(async () => { try { const detail = await api<{ task: { status: string } }>(`/api/agent-tasks/${taskId}`); if (['completed', 'failed', 'rejected', 'expired'].includes(detail.task.status)) { clearInterval(timer); setApprovalStatus(current => ({ ...current, [taskId]: detail.task.status })); } } catch { clearInterval(timer); } }, 1000); };
  const approve = async (taskId: string) => { setApprovalStatus(current => ({ ...current, [taskId]: '写入中' })); try { await api(`/api/agent-tasks/${taskId}/approve`, { method: 'POST' }); poll(taskId); } catch (e) { setApprovalStatus(current => ({ ...current, [taskId]: (e as Error).message })); } };
  const reject = async (taskId: string) => { setApprovalStatus(current => ({ ...current, [taskId]: '拒绝中' })); try { await api(`/api/agent-tasks/${taskId}/reject`, { method: 'POST' }); setApprovalStatus(current => ({ ...current, [taskId]: '已拒绝' })); } catch (e) { setApprovalStatus(current => ({ ...current, [taskId]: (e as Error).message })); } };

  const send = async (initial?: string) => {
    const prompt = (initial ?? requirement).trim(); if (!prompt || busy) return; setBusy(true); setError(''); setRequirement('');
    let current = active;
    if (!current) {
      try {
        const result = await api<{ conversation: Conversation }>('/api/conversations', { method: 'POST', body: JSON.stringify({ title: prompt.slice(0, 30), rootId: agentContext?.rootId }) });
        current = result.conversation; setActive(current); loadConversations();
      } catch (e) { setError((e as Error).message); setBusy(false); return; }
    }
    const conversation = current;
    try {
      setMessages(existing => [...existing, { id: crypto.randomUUID(), role: 'user', content: prompt }]);
      const assistantId = crypto.randomUUID();
      setStreaming(cur => ({ ...cur, [conversation.id]: { id: assistantId, content: '思考中…' } }));
      setMessages(existing => [...existing, { id: assistantId, role: 'assistant', content: '思考中…' }]);
      const response = await fetch('/api/chat/stream', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conversationId: conversation.id, content: prompt, deviceId: agentContext?.deviceId, rootId: agentContext?.rootId, sources: [] }) });
      if (!response.ok || !response.body) {
        let message = '';
        try { const body = await response.json(); message = body?.error ?? ''; } catch { /* 非 JSON 响应 */ }
        throw new Error(message || `服务暂不可用（HTTP ${response.status}），请稍后重试`);
      }
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; let resultTraces: Trace[] = []; let raw = '';
      const consume = (block: string) => {
        const lines = block.split(/\r?\n/); const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim(); const dataLine = lines.find(line => line.startsWith('data:'))?.slice(5).trim(); if (!dataLine) return;
        const data = JSON.parse(dataLine);
        if (event === 'token') {
          raw += data.text ?? '';
          const partial = suggestionFromStream(raw) || '思考中…';
          setStreaming(cur => ({ ...cur, [conversation.id]: { id: assistantId, content: partial } }));
          setMessages(existing => existing.map(m => m.id === assistantId ? { ...m, content: partial } : m));
        } else if (event === 'tool') {
          resultTraces = [...resultTraces, data];
          if (activeIdRef.current === conversation.id) setTraces(resultTraces);
          const statusText = `正在执行 ${TOOL_LABEL[data.name] ?? data.name}…`;
          setStreaming(cur => ({ ...cur, [conversation.id]: { id: assistantId, content: statusText } }));
          setMessages(existing => existing.map(m => m.id === assistantId ? { ...m, content: statusText } : m));
        } else if (event === 'complete') {
          resultTraces = data.toolTrace ?? resultTraces;
          const text = data.content ?? data.generation?.suggestion ?? '';
          setStreaming(cur => { const next = { ...cur }; delete next[conversation.id]; return next; });
          if (activeIdRef.current === conversation.id) { setTraces(resultTraces); setMessages(existing => existing.map(m => m.id === assistantId ? { ...m, content: text } : m)); }
        } else if (event === 'error') throw new Error(data.error);
      };
      while (true) { const chunk = await reader.read(); if (chunk.done) break; buffer += decoder.decode(chunk.value, { stream: true }); const blocks = buffer.split('\n\n'); buffer = blocks.pop() ?? ''; blocks.forEach(consume); }
      if (buffer.trim()) consume(buffer);
    } catch (e) { if (activeIdRef.current === conversation.id) setError((e as Error).message); } finally { setBusy(false); setStreaming(cur => { const next = { ...cur }; delete next[conversation.id]; return next; }); }
  };

  const suggestions = (agentContext ? [
    { icon: '📁', title: '查看项目结构', prompt: '先查看一下当前项目的文件结构，并给我一个项目概览。' },
    { icon: '📖', title: '解释核心代码', prompt: '读取项目中的主要源码，解释它们的职责和关键逻辑。' },
    { icon: '🛠️', title: '帮我改代码', prompt: '检查当前项目代码，找出可以改进的地方并给出修改建议。' },
    { icon: '✨', title: '新增功能', prompt: '在当前项目里新增一个小功能模块，并给出完整的改动补丁。' },
  ] : [
    { icon: '💻', title: '生成组件', prompt: '用 React 写一个带搜索框的表格组件示例。' },
    { icon: '🐍', title: '数据处理', prompt: '用 Python 写一个读取并分析 CSV 文件的脚本。' },
    { icon: '🔍', title: '解释代码', prompt: '用通俗易懂的方式解释一段复杂的代码。' },
    { icon: '📐', title: '项目规划', prompt: '帮我规划一个 Web 项目的基本目录和模块划分。' },
  ]);
  const projects = devices.flatMap(device => device.roots.map(root => ({ deviceId: device.id, root })));
  const groups = projects.map(project => ({ ...project, conversations: conversations.filter(c => c.rootId === project.root.id) }));
  const unbound = conversations.filter(c => !c.rootId || !projects.some(p => p.root.id === c.rootId));
  const activeStreaming = active ? streaming[active.id] : undefined;
  const renderedMessages = activeStreaming && !messages.some(m => m.id === activeStreaming.id) ? [...messages, { id: activeStreaming.id, role: 'assistant', content: activeStreaming.content }] : messages;
  const pending = traces.filter(trace => trace.name === 'stage_patch' && trace.result?.status === 'awaiting_approval' && !approvalStatus[String(trace.result.taskId)]);
  return (
    <div className="shell">
      <aside>
        <div className="brand"><span className="brand-mark">AI</span><span>企业 AI 编程助手</span></div>
        <button className="new" onClick={() => { setActive(null); setMessages([]); setTraces([]); }}><span className="new-icon">+</span>新建对话</button>
        <p className="history-title">历史记录</p>
        <div className="history">
          {groups.map(group => <div className="history-group" key={group.root.id}>
            <button className={!active && agentContext?.rootId === group.root.id ? 'project selected' : 'project'} onClick={() => { setAgentContext({ deviceId: group.deviceId, rootId: group.root.id }); setActive(null); setMessages([]); setTraces([]); }}><span className="project-icon">📁</span><span className="project-name">{group.root.label}</span>{group.conversations.length > 0 && <span className="project-count">{group.conversations.length}</span>}</button>
            {group.conversations.map(conversation => <div className="history-item" key={conversation.id}><button className={active?.id === conversation.id ? 'selected' : ''} onClick={() => open(conversation)}>{conversation.title}</button><button className="history-del" title="删除对话" onClick={event => { event.stopPropagation(); del(conversation); }}>删除</button></div>)}
          </div>)}
          {unbound.length > 0 && <div className="history-group"><div className="project"><span className="project-icon">🗂️</span><span className="project-name">未关联项目</span><span className="project-count">{unbound.length}</span></div>{unbound.map(conversation => <div className="history-item" key={conversation.id}><button className={active?.id === conversation.id ? 'selected' : ''} onClick={() => open(conversation)}>{conversation.title}</button><button className="history-del" title="删除对话" onClick={event => { event.stopPropagation(); del(conversation); }}>删除</button></div>)}</div>}
        </div>
        <AgentPanel context={agentContext} onContext={setAgentContext} devices={devices} locked={Boolean(active?.rootId)} />
        <div className="account"><span className="account-user"><span className="avatar-sm">{user.username.slice(0, 1).toUpperCase()}</span>{user.username}</span><button className="logout" onClick={async () => { await api('/api/auth/logout', { method: 'POST' }); setUser(null); }}>退出</button></div>
      </aside>
      <main className="chat">
        {active && <header><div><p className="eyebrow">AI WORKBENCH</p><h2>{active.title}</h2></div><span className="badge"><i className="badge-dot" />{agentContext ? '已连接本地工程工具' : '未连接本地工程'}</span></header>}
        <section className="messages">
          {renderedMessages.length === 0 && <div className="empty">
            <div className="empty-icon">💻</div>
            <h2>有什么可以帮你的？</h2>
            {agentContext && <span className="empty-badge">已连接本地工程工具</span>}
            <p>{agentContext ? 'AI 助手已连接你的本地 Agent，可以回答问题，也能读取和修改你授权的工程。' : 'AI 可以回答问题；连接本地 Agent 后，还能读取和修改你授权的本地工程。'}</p>
            <div className="suggestions">{suggestions.map(s => <button key={s.title} className="suggestion" onClick={() => send(s.prompt)}><span className="suggestion-icon">{s.icon}</span><span className="suggestion-text"><strong>{s.title}</strong><small>{s.prompt}</small></span></button>)}</div>
            <div className="empty-input">
              <textarea value={requirement} onChange={event => setRequirement(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} placeholder="输入你的问题，或描述想让 AI 处理的工程任务…" />
              <div className="sendrow">{error && <span className="error">{error}</span>}<span className="busy-hint">{busy ? 'AI 工作中…' : ''}</span><button className="send-btn" disabled={busy || !requirement.trim()} onClick={() => send()} title={busy ? '处理中' : '发送'}>{busy ? <span className="spinner" /> : '↑'}</button></div>
              <p className="composer-hint">Enter 发送 · Shift+Enter 换行</p>
            </div>
          </div>}
          {renderedMessages.map(message => <article key={message.id} className={message.role}><div className="avatar">{message.role === 'user' ? '我' : 'AI'}</div><div className="bubble"><div className="msg-name">{message.role === 'user' ? '你' : 'AI 助手'}</div><div className="msg-text">{message.content}</div></div></article>)}
          {traces.map((trace, index) => <ToolCard key={`${trace.name}-${index}`} trace={trace} status={trace.result?.taskId ? approvalStatus[String(trace.result.taskId)] : undefined} onApprove={() => approve(String(trace.result.taskId))} onReject={() => reject(String(trace.result.taskId))} />)}
        </section>
        {pending.length > 0 && <div className="approval-list"><p>以下补丁等待你的确认，确认后才会写入本地工程。</p>{pending.map(trace => <span key={trace.result.taskId}><button onClick={() => approve(String(trace.result.taskId))}>确认写入补丁</button><small>任务 {String(trace.result.taskId).slice(0, 8)}</small></span>)}</div>}
        {renderedMessages.length > 0 && <footer>
          <textarea value={requirement} onChange={event => setRequirement(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} placeholder="输入你的问题，或描述想让 AI 处理的工程任务…" />
          <div className="sendrow">{error && <span className="error">{error}</span>}<span className="busy-hint">{busy ? 'AI 工作中…' : ''}</span><button className="send-btn" disabled={busy || !requirement.trim()} onClick={() => send()} title={busy ? '处理中' : '发送'}>{busy ? <span className="spinner" /> : '↑'}</button></div>
          <p className="composer-hint">Enter 发送 · Shift+Enter 换行</p>
        </footer>}
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<App />);
