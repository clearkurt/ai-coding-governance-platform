import { defineStore } from "pinia";
import { apiRequest } from "../api";

export interface CurrentUser {
  id: string;
  team_id: string;
  email: string;
}

export const useSessionStore = defineStore("session", {
  state: () => ({
    user: null as CurrentUser | null,
    loading: false,
  }),
  actions: {
    async load() {
      this.loading = true;
      try {
        this.user = await apiRequest<CurrentUser>("/auth/me");
        return true;
      } catch {
        this.user = null;
        return false;
      } finally {
        this.loading = false;
      }
    },
    async login(teamId: string, email: string, password: string) {
      await apiRequest<void>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ team_id: teamId, email, password }),
      });
      await this.load();
    },
    async logout() {
      await apiRequest<void>("/auth/logout", { method: "POST" });
      this.user = null;
    },
  },
});
