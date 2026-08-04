import { defineStore } from "pinia";

export type TaskState = "pending" | "running" | "waiting_approval" | "completed" | "failed" | "cancelled";

export interface TaskEvent {
  sequence: number;
  type: string;
  payload: unknown;
}

export const useTaskStore = defineStore("task", {
  state: () => ({ taskId: null as string | null, state: "pending" as TaskState, events: [] as TaskEvent[] }),
  actions: {
    apply(event: TaskEvent) {
      if (this.events.some((item) => item.sequence === event.sequence)) return;
      this.events.push(event);
      if (event.type === "approval.requested") this.state = "waiting_approval";
      if (event.type === "turn.completed") this.state = "completed";
      if (event.type === "turn.failed") this.state = "failed";
      if (event.type === "turn.cancelled") this.state = "cancelled";
    },
  },
});
