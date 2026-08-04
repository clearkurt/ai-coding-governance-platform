import type { RouteRecordRaw } from "vue-router";
import AgentWorkspace from "../views/AgentWorkspace.vue";
import LoginView from "../views/LoginView.vue";

export const routes: RouteRecordRaw[] = [
  { path: "/login", name: "login", component: LoginView },
  { path: "/", name: "workspace", component: AgentWorkspace, meta: { requiresAuth: true } },
];
