import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";
import { create, createDiscreteApi, NAlert, NButton, NCard, NConfigProvider, NEmpty, NForm, NFormItem, NGlobalStyle, NInput, NLayout, NLayoutContent, NLayoutHeader, NSelect, NTag, NTimeline, NTimelineItem } from "naive-ui";
import App from "./App.vue";
import { routes } from "./router/routes";
import { useSessionStore } from "./stores/session";
import "./styles.css";

const router = createRouter({ history: createWebHistory(), routes });
const pinia = createPinia();
router.beforeEach(async (to) => {
  const session = useSessionStore(pinia);
  if (to.meta.requiresAuth && !session.user && !(await session.load())) return { name: "login" };
  if (to.name === "login" && (session.user || await session.load())) return { name: "workspace" };
  return true;
});
const { message } = createDiscreteApi(["message"]);
const naive = create({ components: [NAlert, NButton, NCard, NConfigProvider, NEmpty, NForm, NFormItem, NGlobalStyle, NInput, NLayout, NLayoutContent, NLayoutHeader, NSelect, NTag, NTimeline, NTimelineItem] });
const app = createApp(App);
app.provide("message", message);
app.use(pinia);
app.use(naive);
app.use(router);
app.mount("#app");
