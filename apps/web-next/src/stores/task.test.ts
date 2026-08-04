import { beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useTaskStore } from "./task";

describe("task store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("deduplicates persisted task event sequences", () => {
    const task = useTaskStore();
    task.apply({ sequence: 1, type: "approval.requested", payload: {} });
    task.apply({ sequence: 1, type: "approval.requested", payload: {} });
    expect(task.events).toHaveLength(1);
    expect(task.state).toBe("waiting_approval");
  });
});
