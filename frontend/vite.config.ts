import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the app on :5173 and forwards everything server-side to the
// FastAPI service on :8000, so sign-in redirects and cookies behave as in production.
const backend = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  server: {
    proxy: Object.fromEntries(
      ["/api", "/auth/login", "/auth/callback", "/auth/platform", "/oauth", "/.well-known", "/mcp"]
        .map((prefix) => [prefix, { target: backend, changeOrigin: false }]),
    ),
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
