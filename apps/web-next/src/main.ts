import { createApp } from "vue";
import { createPinia } from "pinia";
import { createRouter, createWebHistory } from "vue-router";
import { createDiscreteApi } from "naive-ui";
import App from "./App.vue";
import { routes } from "./router/routes";
import "./styles.css";

const router = createRouter({ history: createWebHistory(), routes });
const { message } = createDiscreteApi(["message"]);
const app = createApp(App);
app.provide("message", message);
app.use(createPinia());
app.use(router);
app.mount("#app");
