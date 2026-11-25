import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const apiUrl = new URL(env.VITE_API_BASE_URL || "http://127.0.0.1:8000");
  if (
    !["http:", "https:"].includes(apiUrl.protocol)
    || !apiUrl.hostname
    || apiUrl.username
    || apiUrl.password
    || apiUrl.pathname !== "/"
    || apiUrl.search
    || apiUrl.hash
  ) {
    throw new Error("VITE_API_BASE_URL must be an HTTP(S) origin without a path");
  }

  const contentSecurityPolicy = (webSocketOrigin = "") => [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      `connect-src 'self' ${apiUrl.origin}${webSocketOrigin ? ` ${webSocketOrigin}` : ""}`,
      "img-src 'self' data:",
      "object-src 'none'",
      "base-uri 'none'",
      "frame-ancestors 'none'",
    ].join("; ");
  const headers = {
    "Content-Security-Policy": contentSecurityPolicy("ws://127.0.0.1:8001"),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
  };
  const previewHeaders = {
    ...headers,
    "Content-Security-Policy": contentSecurityPolicy(),
  };

  return {
    plugins: [react()],
    server: { strictPort: true, headers },
    preview: { strictPort: true, headers: previewHeaders },
  };
});
