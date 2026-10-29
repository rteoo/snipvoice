import { defineConfig } from "vite";

// Tauri serves the built files itself; the dev server only backs `tauri dev`
// and the browser preview with the mock bridge.
export default defineConfig({
  clearScreen: false,
  server: { port: 1420, strictPort: true, host: "127.0.0.1" },
  build: { target: "es2022", outDir: "dist", emptyOutDir: true },
});
