import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Internal workspace packages ship raw TS source; let Next compile them.
  transpilePackages: ["@spool/types", "@spool/ui", "@spool/api-client", "@spool/mcp-client"],
};

export default nextConfig;
