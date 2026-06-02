import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Internal workspace packages ship raw TS source; let Next compile them.
  transpilePackages: ["@spool/types", "@spool/ui", "@spool/api-client", "@spool/mcp-client"],
  // The dev indicator portal overlaps the top-right chrome (and intercepts e2e clicks); off.
  devIndicators: false,
};

export default nextConfig;
