import { defineStore } from "pinia";
import { apiRequest } from "../api";

export interface ProjectRecord {
  id: string;
  device_id: string;
  root_id: string;
  display_name: string;
}

export interface DeviceRecord {
  id: string;
  name: string;
  runtime_version: string | null;
  last_seen_at: string | null;
  online: boolean;
  projects: ProjectRecord[];
}

export interface ConversationRecord {
  id: string;
  title: string;
  created_at: string;
}

export const useResourceStore = defineStore("resources", {
  state: () => ({
    devices: [] as DeviceRecord[],
    conversations: [] as ConversationRecord[],
    loading: false,
  }),
  actions: {
    async load() {
      this.loading = true;
      try {
        [this.devices, this.conversations] = await Promise.all([
          apiRequest<DeviceRecord[]>("/devices"),
          apiRequest<ConversationRecord[]>("/conversations"),
        ]);
      } finally {
        this.loading = false;
      }
    },
    async createConversation(title: string) {
      const conversation = await apiRequest<ConversationRecord>("/conversations", {
        method: "POST",
        body: JSON.stringify({ title }),
      });
      this.conversations.unshift(conversation);
      return conversation;
    },
    async createPairingCode() {
      return apiRequest<{ code: string; expires_at: string }>("/pairing-codes", {
        method: "POST",
      });
    },
  },
});
