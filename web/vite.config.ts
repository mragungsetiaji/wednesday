import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, API calls go to the Python server (uv run wednesday --serve).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.XAU_API ?? "http://127.0.0.1:8000",
        // The server refuses a write whose Origin isn't its host: tell it the page's host (the dev server's).
        configure: (proxy) => {
          proxy.on("proxyReq", (req, incoming) => {
            if (incoming.headers.host) req.setHeader("x-forwarded-host", incoming.headers.host);
          });
        },
      },
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 1000 },
});
