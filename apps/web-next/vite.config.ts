import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/auth": "http://127.0.0.1:8081",
      "/pairing-codes": "http://127.0.0.1:8081",
      "/devices": "http://127.0.0.1:8081",
      "/projects": "http://127.0.0.1:8081",
      "/conversations": "http://127.0.0.1:8081",
      "/tasks": "http://127.0.0.1:8081",
      "/approvals": "http://127.0.0.1:8081",
      "/v1": "http://127.0.0.1:8081",
    },
  },
});
