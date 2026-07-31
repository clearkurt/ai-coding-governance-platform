export type AgentTaskResult = { status: string; result?: unknown; error?: string };

type Waiter = { resolve: (value: AgentTaskResult) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> };

/** In-memory correlation between an API tool call and an Agent task result. */
export class AgentTaskBridge {
  private readonly waiters = new Map<string, Waiter>();

  wait(taskId: string, timeoutMs = 10 * 60_000): Promise<AgentTaskResult> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters.delete(taskId);
        reject(new Error(`Agent task timed out: ${taskId}`));
      }, timeoutMs);
      this.waiters.set(taskId, { resolve, reject, timer });
    });
  }

  complete(taskId: string, result: AgentTaskResult): boolean {
    const waiter = this.waiters.get(taskId);
    if (!waiter) return false;
    clearTimeout(waiter.timer);
    this.waiters.delete(taskId);
    waiter.resolve(result);
    return true;
  }

  fail(taskId: string, error: Error): boolean {
    const waiter = this.waiters.get(taskId);
    if (!waiter) return false;
    clearTimeout(waiter.timer);
    this.waiters.delete(taskId);
    waiter.reject(error);
    return true;
  }
}
