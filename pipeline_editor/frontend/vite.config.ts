import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-antd": ["antd", "@ant-design/icons"],
          "vendor-flow": ["@xyflow/react", "@dagrejs/dagre"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8930",
      "/ws": { target: "ws://127.0.0.1:8930", ws: true },
    },
  },
  test: {
    environment: "jsdom",
  },
});
