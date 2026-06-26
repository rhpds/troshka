import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL || "http://localhost:8200";

const nextConfig: NextConfig = {
  ...(process.env.STANDALONE === "true" && { output: "standalone" }),
  skipTrailingSlashRedirect: true,
  async rewrites() {
    return [
      {
        source: "/ws/:path*",
        destination: `${backendUrl}/ws/:path*`,
      },
    ];
  },
};

export default nextConfig;
