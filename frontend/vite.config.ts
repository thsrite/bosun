import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // 绑 0.0.0.0：局域网设备(手机等)可访问 http://<本机IP>:5199
    port: 5199,
    proxy: {
      "/api": "http://127.0.0.1:8770",
      "/ws": { target: "ws://127.0.0.1:8770", ws: true },
    },
  },
});
