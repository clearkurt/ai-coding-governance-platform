export interface Generation { analysis: string; suggestion: string; code: string; cautions: string[]; }
export interface ModelInput { requirement: string; codeStyle: string; sources: Array<{name:string;content:string}>; }
export interface ModelProvider { generate(input: ModelInput): Promise<Generation>; }
export type AgentToolName = 'list_files' | 'read_file' | 'stage_patch' | 'apply_patch';
export interface AgentToolCall { name: AgentToolName; arguments: Record<string, unknown>; }
export interface AgentTurn { kind: 'tool_call' | 'final'; toolCall?: AgentToolCall; generation?: Generation; }
export interface AgentToolInput extends ModelInput { toolResults?: Array<{ call: AgentToolCall; result: unknown }>; }
export interface AgentModelProvider extends ModelProvider { plan(input: AgentToolInput): Promise<AgentTurn>; }
export type AgentToolExecutor = (call: AgentToolCall) => Promise<unknown>;

export async function runAgentLoop(provider: AgentModelProvider, input: ModelInput, execute: AgentToolExecutor, maxTurns = 8, onTool?: (call: AgentToolCall, result: unknown) => void): Promise<Generation> {
  let toolResults: Array<{ call: AgentToolCall; result: unknown }> = [];
  for (let turn = 0; turn < maxTurns; turn += 1) {
    const next = await provider.plan({ ...input, toolResults });
    if (next.kind === 'final' && next.generation) return next.generation;
    if (next.kind !== 'tool_call' || !next.toolCall) throw new Error('模型返回了无效的工具调用');
    const result = await execute(next.toolCall);
    onTool?.(next.toolCall, result);
    toolResults = [...toolResults, { call: next.toolCall, result }];
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

/** Calls an OpenAI-compatible company model gateway. */
export class OpenAICompatibleProvider implements AgentModelProvider {
  constructor(private readonly baseUrl: string, private readonly apiKey: string, private readonly model: string) {}

  async generate(input: ModelInput): Promise<Generation> {
    const turn = await this.plan(input);
    if (turn.kind !== 'final' || !turn.generation) throw new Error('模型需要调用本地工具后才能生成结果');
    return turn.generation;
  }

  async plan(input: AgentToolInput): Promise<AgentTurn> {
    const response = await fetch(`${this.baseUrl.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${this.apiKey}` },
      body: JSON.stringify({ model: this.model, temperature: 0.2, response_format: { type: 'json_object' }, messages: [
        { role: 'system', content: `你是企业内部 C 代码 Agent。必须遵守以下规范：\n${input.codeStyle}\n你可以通过 list_files、read_file、stage_patch、apply_patch 工具直接操作授权工程；禁止请求 Shell、编译器或任意进程。所有写入仍由 Agent 校验路径、原文件哈希和企业写入策略。只返回 JSON：{"kind":"tool_call","toolCall":{"name":"read_file","arguments":{}}} 或 {"kind":"final","generation":{"analysis":string,"suggestion":string,"code":string,"cautions":string[]}}。` },
        { role: 'user', content: JSON.stringify({ requirement: input.requirement, sources: input.sources, toolResults: input.toolResults ?? [] }) }
      ] })
    });
    if (!response.ok) throw new Error(`模型网关请求失败：HTTP ${response.status}`);
    const body = await response.json() as ChatResponse;
    const content = body.choices?.[0]?.message?.content;
    if (!content) throw new Error('模型网关未返回内容');
    const parsed = JSON.parse(content) as Partial<AgentTurn>;
    if (parsed.kind === 'tool_call' && parsed.toolCall && ['list_files','read_file','stage_patch','apply_patch'].includes(parsed.toolCall.name)) return { kind: 'tool_call', toolCall: parsed.toolCall };
    const generation = parsed.generation as Partial<Generation> | undefined;
    if (parsed.kind !== 'final' || !generation || typeof generation.analysis !== 'string' || typeof generation.suggestion !== 'string' || typeof generation.code !== 'string' || !Array.isArray(generation.cautions)) throw new Error('模型返回格式无效');
    return { kind: 'final', generation: { analysis: generation.analysis, suggestion: generation.suggestion, code: generation.code, cautions: generation.cautions.filter((x): x is string => typeof x === 'string') } };
  }
}
