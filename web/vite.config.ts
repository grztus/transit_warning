import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "");
  const backendTarget = environment.VITE_BACKEND_TARGET
    || "http://127.0.0.1:8765";

  return {
    plugins: [react()],
    server: {
      proxy: {
        "/api": backendTarget,
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      restoreMocks: true,
    },
  };
});
