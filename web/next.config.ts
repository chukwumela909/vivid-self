import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained build for Docker: emits .next/standalone with a minimal
  // node server and only the deps actually used at runtime.
  output: "standalone",
};

export default nextConfig;
