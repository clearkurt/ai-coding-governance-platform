import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useResourceStore } from "./resources";

describe("resource store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.unstubAllGlobals();
  });

  it("loads devices and conversations and creates a conversation", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([{ id: "device-1", projects: [] }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{ id: "conversation-1", title: "Existing" }]), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "conversation-2", title: "New", created_at: "now" }), { status: 201 }),
      );
    vi.stubGlobal("fetch", fetch);
    const resources = useResourceStore();

    await resources.load();
    const created = await resources.createConversation("New");

    expect(resources.devices[0]?.id).toBe("device-1");
    expect(resources.conversations.map((item) => item.id)).toEqual(["conversation-2", "conversation-1"]);
    expect(created.title).toBe("New");
    expect(fetch).toHaveBeenCalledTimes(3);
  });
});
