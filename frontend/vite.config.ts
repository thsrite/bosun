import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (id.includes("@xterm")) return "vendor-xterm";
          if (id.includes("recharts") || id.includes("d3-")) return "vendor-charts";
          if (id.includes("react-markdown") || id.includes("remark") || id.includes("rehype") || id.includes("unified") || id.includes("mdast")) return "vendor-markdown";
          if (id.includes("@dnd-kit")) return "vendor-dnd";
          if (id.includes("react-dom") || id.includes("/react/")) return "vendor-react";
        },
      },
    },
  },
  server: {
    host: true, // 绑 0.0.0.0：局域网设备(手机等)可访问 http://<本机IP>:5199
    port: 5199,
    proxy: {
      "/api": "http://127.0.0.1:8770",
      "/ws": { target: "ws://127.0.0.1:8770", ws: true },
    },
  },
});
