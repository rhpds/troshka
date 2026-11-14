import type { NextConfig } from "next";
import { execFileSync } from "node:child_process";

function buildVersion(): string {
  if (process.env.TROSHKA_BUILD_VERSION) return process.env.TROSHKA_BUILD_VERSION;
  try {
    return execFileSync("git", ["describe", "--tags", "--match", "v*", "--always", "--dirty"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return "Development build";
  }
}

const backendUrl = process.env.BACKEND_URL || "http://localhost:8200";

const nextConfig: NextConfig = {
  env: {
    NEXT_PUBLIC_TROSHKA_VERSION: buildVersion(),
  },
  ...(process.env.STANDALONE === "true" && { output: "standalone" }),
  skipTrailingSlashRedirect: true,
  async rewrites() {
    return {
      beforeFiles: [
        {
          source: "/api/v1/projects/:id/ws",
          destination: `${backendUrl}/api/v1/projects/:id/ws`,
        },
        {
          source: "/api/v1/patterns/:id/ws",
          destination: `${backendUrl}/api/v1/patterns/:id/ws`,
        },
        {
          source: "/ws/:path*",
          destination: `${backendUrl}/ws/:path*`,
        },
      ],
      afterFiles: [],
      fallback: [],
    };
  },
};

export default nextConfig;
// v2
