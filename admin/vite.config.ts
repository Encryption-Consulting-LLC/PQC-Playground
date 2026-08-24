import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Backend (FastAPI) dev server. Override with VITE_API_TARGET if needed.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000"

// The built admin app is served same-origin by the backend under /admin
// (app/main.py::_mount_admin) — base must match so asset URLs resolve there.
// https://vite.dev/config/
export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      // The remote-desktop viewer is shared with the operator app rather than
      // ported. `lib/ws.ts` is duplicated between the two apps deliberately — it
      // is small and the copies have diverged on purpose — but a Guacamole
      // protocol client is too large to keep in step by hand. Both apps run the
      // same React, Tailwind and Base UI versions, so one file serves both.
      "@shared": path.resolve(__dirname, "../frontend/src"),
    },
  },
  server: {
    port: 5433,
    proxy: {
      // Admin calls /api/*; backend serves under /api so no rewrite needed.
      // `ws: true` also forwards the WebSocket upgrade, matching frontend/.
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
