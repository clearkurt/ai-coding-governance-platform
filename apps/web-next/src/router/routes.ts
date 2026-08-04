import type { RouteRecordRaw } from "vue-router";
import AgentWorkspace from "../views/AgentWorkspace.vue";

export const routes: RouteRecordRaw[] = [
  { path: "/", name: "workspace", component: AgentWorkspace },
];
