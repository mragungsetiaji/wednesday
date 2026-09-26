import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, API calls go to the Python server (uv run wednesday --serve).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": process.env.XAU_API ?? "http://127.0.0.1:8000" },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 1000 },
});
