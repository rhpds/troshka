import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL || "http://localhost:8200";

const nextConfig: NextConfig = {
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
