import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8200/api/:path*",
      },
      {
        source: "/ws/:path*",
        destination: "http://localhost:8200/ws/:path*",
      },
    ];
  },
};

export default nextConfig;
