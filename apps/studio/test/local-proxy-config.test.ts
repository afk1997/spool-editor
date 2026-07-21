import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

async function studioFile(relativePath: string): Promise<string> {
  return readFile(resolve(process.cwd(), relativePath), "utf8");
}

describe("Studio local proxy configuration", () => {
  it("binds development and production servers to IPv4 loopback", async () => {
    const packageJson = JSON.parse(await studioFile("package.json")) as {
      scripts: Record<string, string>;
    };
    expect(packageJson.scripts.dev).toBe("next dev --hostname 127.0.0.1");
    expect(packageJson.scripts.start).toBe("next start --hostname 127.0.0.1");
  });

  it("documents server-only engine variables without a public token", async () => {
    const example = await studioFile(".env.example");
    expect(example).toContain("SPOOL_ENGINE_URL=http://127.0.0.1:8899");
    expect(example).toContain("SPOOL_ENGINE_TOKEN=");
    expect(example).not.toContain("NEXT_PUBLIC_");
  });

  it("constructs the browser singleton against the same-origin proxy", async () => {
    const engineClient = await studioFile("src/lib/engine.ts");
    expect(engineClient).toContain('baseUrl: "/api/engine"');
    expect(engineClient).not.toContain("NEXT_PUBLIC_");
    expect(engineClient).not.toContain("127.0.0.1:8899");
  });

  it("keeps smoke helper engine URL semantics distinct and supports bearer auth", async () => {
    const smoke = await studioFile("scripts/smoke.mjs");
    expect(smoke).toContain("process.env.E2E_ENGINE_API_URL");
    expect(smoke).toContain("process.env.TROVE_TOKEN");
    expect(smoke).toMatch(/Authorization[^\n]+Bearer/);
    expect(smoke).not.toContain("process.env.SPOOL_ENGINE_URL");
  });

  it("warns that the bearer-injecting Studio route must remain local-only", async () => {
    const readme = await readFile(resolve(process.cwd(), "../..", "README.md"), "utf8");
    expect(readme).toMatch(/loopback-only/i);
    expect(readme).toMatch(/reverse proxy/i);
  });
});
