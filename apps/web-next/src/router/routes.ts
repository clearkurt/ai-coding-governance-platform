import type { RouteRecordRaw } from "vue-router";

export const routes: RouteRecordRaw[] = [
  { path: "/login", name: "login", component: () => import("../views/LoginView.vue") },
  {
    path: "/",
    name: "workspace",
    component: () => import("../views/AgentWorkspace.vue"),
    meta: { requiresAuth: true },
  },
];
