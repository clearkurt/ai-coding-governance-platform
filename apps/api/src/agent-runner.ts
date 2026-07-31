import { runAgentLoop, type AgentModelProvider, type AgentToolCall, type ModelInput, type Generation } from './model.js';

export type AgentTaskDispatcher = (call: AgentToolCall) => Promise<unknown>;

/** Runs the server-side LLM agent against the local Agent task bridge. */
export function runServerAgent(provider: AgentModelProvider, input: ModelInput, dispatch: AgentTaskDispatcher, maxTurns = 8, onTool?: (call: AgentToolCall, result: unknown) => void): Promise<Generation> {
  return runAgentLoop(provider, input, dispatch, maxTurns, onTool);
}
