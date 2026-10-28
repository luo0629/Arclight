import type { NextConfig } from "next";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const nextConfig: NextConfig = {
  output: process.env.ARCLIGHT_STANDALONE === "1" ? "standalone" : undefined,
  poweredByHeader: false,
  reactStrictMode: true,
  turbopack: { root: dirname(fileURLToPath(import.meta.url)) },
};

export default nextConfig;
