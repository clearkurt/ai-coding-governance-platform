import { readFileSync } from 'node:fs';

export interface Generation { analysis: string; suggestion: string; code: string; cautions: string[]; }
export interface ModelInput { requirement: string; codeStyle: string; sources: Array<{name:string;content:string}>; history?: ChatMessage[]; toolsAvailable?: boolean; projectRules?: string; }
export interface ModelProvider { generate(input: ModelInput): Promise<Generation>; }
export type AgentToolName = 'list_files' | 'read_file' | 'stage_patch' | 'apply_patch' | 'run_command';
export interface AgentToolCall { name: AgentToolName; arguments: Record<string, unknown>; }
export interface AgentTurn { kind: 'tool_call' | 'final'; toolCall?: AgentToolCall; generation?: Generation; }
export interface AgentToolInput extends ModelInput { toolResults?: Array<{ call: AgentToolCall; result: unknown }>; }
export interface AgentModelProvider extends ModelProvider { plan(input: AgentToolInput, onDelta?: (delta: string) => void): Promise<AgentTurn>; }
export type AgentToolExecutor = (call: AgentToolCall) => Promise<unknown>;

const toolArgumentSchemas: Record<AgentToolName, z.ZodType<Record<string, unknown>>> = {
  list_files: z.object({ relativePath: z.string().max(500).default('') }).passthrough(),
  read_file: z.object({ relativePath: z.string().min(1).max(500) }).passthrough(),
  stage_patch: z.object({ relativePath: z.string().min(1).max(500), originalSha256: z.string().regex(/^[a-f0-9]{64}$/), newContent: z.string().max(1024 * 1024) }).passthrough(),
  apply_patch: z.object({ relativePath: z.string().min(1).max(500), originalSha256: z.string().regex(/^[a-f0-9]{64}$/), newContent: z.string().max(1024 * 1024), approvalToken: z.string().min(16).max(200) }).passthrough(),
  run_command: z.object({ command: z.string().min(1).max(2000), cwd: z.string().max(500).default('') }).passthrough()
};

export function validateAgentToolCall(call: AgentToolCall): AgentToolCall {
  const schema = toolArgumentSchemas[call.name];
  if (!schema) throw new Error(`不支持的 Agent 工具: ${String(call.name)}`);
  const parsed = schema.safeParse(call.arguments);
  if (!parsed.success) throw new Error(`Agent 工具参数无效: ${parsed.error.issues[0]?.message ?? 'unknown'}`);
  return { name: call.name, arguments: parsed.data };
}

export async function runAgentLoop(provider: AgentModelProvider, input: ModelInput, execute: AgentToolExecutor, maxTurns = 8, onTool?: (call: AgentToolCall, result: unknown) => void, onDelta?: (delta: string) => void): Promise<Generation> {
  let toolResults: Array<{ call: AgentToolCall; result: unknown }> = [];
  for (let turn = 0; turn < maxTurns; turn += 1) {
    const next = await provider.plan({ ...input, toolResults }, onDelta);
    if (next.kind === 'final' && next.generation) return next.generation;
    if (next.kind !== 'tool_call' || !next.toolCall) throw new Error('模型返回了无效的工具调用');
    const call = validateAgentToolCall(next.toolCall);
    const result = await execute(call);
    onTool?.(call, result);
    toolResults = [...toolResults, { call, result }];
  }
  throw new Error(`模型工具调用超过 ${maxTurns} 回合限制`);
}

export class MockModelProvider implements ModelProvider {
  async generate(input: ModelInput): Promise<Generation> {
    await new Promise(resolve => setTimeout(resolve, 650));
    const context = input.sources.length ? `已分析：${input.sources.map(f => f.name).join('、')}` : '未提供现有源码，以下为独立草案';
    return { analysis: `${context}。需求为：${input.requirement}`, suggestion: '建议先在目标板上逐段验证 LCD 映射，再合入主工程。', code: `/** ${input.requirement} */\nvoid lcd_update_display(void)\n{\n    /* TODO: 根据已确认的 SEG/COM 映射写入 LCD RAM。 */\n}\n`, cautions: ['请确认芯片型号与 LCD COM 数。', '请在真机验证每个符号和数字笔段。'] };
  }
}

type ChatResponse = { choices?: Array<{ message?: { content?: string } }> };
export type ChatMessage = { role: 'user' | 'assistant' | 'system'; content: string };

/** Calls an OpenAI-compatible company model gateway. */
export class OpenAICompatibleProvider implements AgentModelProvider {
  private readonly agentPrompt: string;
  constructor(private readonly baseUrl: string, private readonly apiKey: string, private readonly model: string, agentPromptPath?: string) {
    this.agentPrompt = agentPromptPath ? readFileSync(agentPromptPath, 'utf8') : 'Respond in JSON format.';
  }

  async chat(messages: ChatMessage[], onDelta?: (delta: string) => void): Promise<string> {
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 90_000); let response: Response;
    try { response = await fetch(`${this.baseUrl.replace(/\/$/, '')}/chat/completions`, { method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${this.apiKey}` }, signal: controller.signal, body: JSON.stringify({ model: this.model, temperature: 0.7, stream: Boolean(onDelta), messages }) }); }
    catch (error) { throw new Error(error instanceof Error && error.name === 'AbortError' ? '模型请求超时' : `模型网关连接失败: ${error instanceof Error ? error.message : 'unknown'}`); }
    finally { clearTimeout(timeout); }
    if (!response.ok) { let detail = ''; try { detail = ': ' + (await response.text()).slice(0, 500); } catch { /* ignore */ } throw new Error(`模型网关请求失败：HTTP ${response.status}${detail}`); }
    let content = '';
    if (onDelta && response.body) { const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''; while (true) { const chunk = await reader.read(); if (chunk.done) break; buffer += decoder.decode(chunk.value, { stream: true }); const lines = buffer.split(/\r?\n/); buffer = lines.pop() ?? ''; for (const line of lines) { if (!line.startsWith('data:')) continue; const value = line.slice(5).trim(); if (!value || value === '[DONE]') continue; const delta = (JSON.parse(value) as { choices?: Array<{ delta?: { content?: string } }> }).choices?.[0]?.delta?.content ?? ''; if (delta) { content += delta; onDelta(delta); } } } }
    else { content = (await response.json() as ChatResponse).choices?.[0]?.message?.content ?? ''; }
    if (!content) throw new Error('模型没有返回内容'); return content;
  }

  async generate(input: ModelInput): Promise<Generation> {
    const turn = await this.plan(input);
    if (turn.kind !== 'final' || !turn.generation) throw new Error('模型需要调用本地工具后才能生成结果');
    return turn.generation;
  }

  async plan(input: AgentToolInput, onDelta?: (delta: string) => void): Promise<AgentTurn> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 90_000);
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${this.apiKey}` },
      signal: controller.signal,
      body: JSON.stringify({ model: this.model, temperature: 0.2, stream: Boolean(onDelta), response_format: { type: 'json_object' }, messages: [
        { role: 'system', content: this.agentPrompt },
        ...(input.history ?? []),
        { role: 'user', content: JSON.stringify({ requirement: input.requirement, codeStyle: input.codeStyle, projectRules: input.projectRules ?? '', sources: input.sources, toolResults: input.toolResults ?? [], toolsAvailable: input.toolsAvailable ?? true }) }
      ] })
      });
    } catch (error) {
      throw new Error(error instanceof Error && error.name === 'AbortError' ? '模型请求超时' : `模型网关连接失败: ${error instanceof Error ? error.message : 'unknown'}`);
    } finally { clearTimeout(timeout); }
    if (!response.ok) { let detail = ''; try { detail = ': ' + (await response.text()).slice(0, 500); } catch { /* ignore */ } throw new Error(`模型网关请求失败：HTTP ${response.status}${detail}`); }
    let content = '';
    if (onDelta && response.body) {
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (true) { const chunk = await reader.read(); if (chunk.done) break; buffer += decoder.decode(chunk.value, { stream: true }); const lines = buffer.split(/\r?\n/); buffer = lines.pop() ?? ''; for (const line of lines) { if (!line.startsWith('data:')) continue; const value = line.slice(5).trim(); if (!value || value === '[DONE]') continue; try { const delta = (JSON.parse(value) as { choices?: Array<{ delta?: { content?: string } }> }).choices?.[0]?.delta?.content ?? ''; if (delta) { content += delta; onDelta(delta); } } catch { /* ignore incomplete gateway chunks */ } } }
    } else { const body = await response.json() as ChatResponse; content = body.choices?.[0]?.message?.content ?? ''; }
    if (!content) throw new Error('模型网关未返回内容');
    let parsed: Partial<AgentTurn>;
    try { parsed = JSON.parse(content) as Partial<AgentTurn>; } catch { throw new Error(`模型返回了非 JSON 内容：${content.slice(0, 500)}`); }
    if (parsed.kind === 'tool_call' && parsed.toolCall) return { kind: 'tool_call', toolCall: validateAgentToolCall(parsed.toolCall as AgentToolCall) };
    const generation = parsed.generation as Partial<Generation> | undefined;
    if (parsed.kind !== 'final' || !generation || typeof generation.analysis !== 'string' || typeof generation.suggestion !== 'string' || typeof generation.code !== 'string' || !Array.isArray(generation.cautions)) throw new Error(`模型返回格式无效，收到：${JSON.stringify(parsed).slice(0, 500)}`);
    return { kind: 'final', generation: { analysis: generation.analysis, suggestion: generation.suggestion, code: generation.code, cautions: generation.cautions.filter((x): x is string => typeof x === 'string') } };
  }
}
import { z } from 'zod';
