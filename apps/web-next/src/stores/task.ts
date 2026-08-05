import { defineStore } from "pinia";
import { apiBase, apiRequest } from "../api";

export type TaskState =
  | "idle"
  | "pending"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export interface TaskEvent {
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
}

export interface TaskRecord {
  id: string;
  root_id: string;
  status: string;
}

export interface ApprovalRecord {
  id: string;
  provider_item_id: string;
  status: "pending" | "approved" | "rejected";
}

export interface RollbackRecord {
  task_id: string;
  status: "requested" | "succeeded" | "failed";
  delivery_id: string;
  created: boolean;
}

export function stateAfterEvent(current: TaskState, type: string): TaskState {
  if (type.includes("requestApproval")) return "waiting_approval";
  if (type === "turn/started" || type === "item/started") return "running";
  if (type === "turn/completed") return "completed";
  if (type === "turn/cancelled") return "cancelled";
  if (type === "turn/failed" || type === "error") return "failed";
  return current;
}

const eventTypes = [
  "thread/started",
  "turn/started",
  "turn/completed",
  "turn/failed",
  "turn/cancelled",
  "item/started",
  "item/completed",
  "item/agentMessage/delta",
  "item/commandExecution/outputDelta",
  "item/fileChange/patchUpdated",
  "item/commandExecution/requestApproval",
  "item/fileChange/requestApproval",
  "item/permissions/requestApproval",
  "error",
];

export const useTaskStore = defineStore("task", {
  state: () => ({
    taskId: null as string | null,
    state: "idle" as TaskState,
    events: [] as TaskEvent[],
    lastSequence: 0,
    stream: null as EventSource | null,
    error: null as string | null,
    approvals: [] as ApprovalRecord[],
    rollbackStatus: null as RollbackRecord["status"] | null,
  }),
  actions: {
    apply(event: TaskEvent) {
      if (
        event.sequence <= this.lastSequence ||
        this.events.some((item) => item.sequence === event.sequence)
      ) {
        return;
      }
      this.events.push(event);
      this.events.sort((a, b) => a.sequence - b.sequence);
      this.lastSequence = Math.max(this.lastSequence, event.sequence);
      this.state = stateAfterEvent(this.state, event.type);
      if (this.state !== "waiting_approval") this.error = null;
    },
    async create(input: {
      deviceId: string;
      projectId: string;
      conversationId: string;
      prompt: string;
    }) {
      this.disconnect();
      this.events = [];
      this.approvals = [];
      this.rollbackStatus = null;
      this.lastSequence = 0;
      this.error = null;
      this.state = "pending";
      const task = await apiRequest<TaskRecord>("/tasks", {
        method: "POST",
        body: JSON.stringify({
          device_id: input.deviceId,
          project_id: input.projectId,
          conversation_id: input.conversationId,
          prompt: input.prompt,
          idempotency_key: crypto.randomUUID(),
        }),
      });
      this.taskId = task.id;
      this.connect();
      return task;
    },
    connect() {
      if (!this.taskId) return;
      this.stream?.close();
      const source = new EventSource(`${apiBase}/tasks/${this.taskId}/events`, {
        withCredentials: true,
      });
      source.onmessage = (message) => this.consume(message);
      for (const type of eventTypes) {
        source.addEventListener(type, (message) => this.consume(message as MessageEvent, type));
      }
      source.onopen = () => {
        this.error = null;
      };
      source.onerror = () => {
        this.error = "事件流暂时中断，浏览器将自动重连";
      };
      this.stream = source;
    },
    consume(message: MessageEvent, explicitType?: string) {
      try {
        const value = JSON.parse(message.data) as {
          sequence: number;
          payload?: Record<string, unknown>;
        };
        const type = explicitType ?? message.type;
        this.apply({ sequence: value.sequence, type, payload: value.payload ?? {} });
        if (type.includes("requestApproval")) void this.refreshApprovals();
      } catch {
        this.error = "收到无法解析的任务事件";
      }
    },
    async cancel() {
      if (!this.taskId) return;
      await apiRequest<void>(`/tasks/${this.taskId}/cancel`, { method: "POST" });
      this.state = "cancelled";
    },
    async decide(approvalId: string, decision: "approved" | "rejected") {
      await apiRequest<void>(`/approvals/${approvalId}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await this.refreshApprovals();
      if (!this.approvals.some((item) => item.status === "pending")) this.state = "running";
    },
    async refreshApprovals() {
      if (!this.taskId) return;
      this.approvals = await apiRequest<ApprovalRecord[]>(`/tasks/${this.taskId}/approvals`);
    },
    async rollback() {
      if (!this.taskId) return;
      const result = await apiRequest<RollbackRecord>(`/tasks/${this.taskId}/rollback`, {
        method: "POST",
      });
      this.rollbackStatus = result.status;
    },
    disconnect() {
      this.stream?.close();
      this.stream = null;
    },
  },
});
