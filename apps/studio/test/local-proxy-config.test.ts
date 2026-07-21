import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { promisify } from "node:util";

import { describe, expect, it } from "vitest";

const execFileAsync = promisify(execFile);

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

  it("requires the isolated external E2E harness and its full real-pipeline timeout", async () => {
    const config = await studioFile("playwright.config.ts");
    expect(config).toContain("timeout: 1_800_000");
    expect(config).toContain("process.env.SPOOL_STUDIO_URL");
    expect(config).not.toMatch(/SPOOL_STUDIO_URL\s*\?\?/);

    const goldenFlow = await studioFile("e2e/url-to-clip.spec.ts");
    expect(goldenFlow).toContain("process.env.E2E_ENGINE_API_URL");
    expect(goldenFlow).toContain("process.env.TROVE_TOKEN");
    expect(goldenFlow).not.toContain("process.env.SPOOL_ENGINE_URL");
    expect(goldenFlow).toContain("AbortSignal.timeout(15_000)");
    expect(goldenFlow).toContain("phase0_range_probe");
    expect(goldenFlow).toContain('cache: "no-store"');
    expect(goldenFlow).toContain('headers()["if-range"]');
    expect(goldenFlow).toContain("videoWidth");
    expect(goldenFlow).toContain("videoHeight");
    expect(goldenFlow).toContain('execFileAsync("ffprobe"');
    expect(goldenFlow).toContain('execFileAsync("ffmpeg"');
    expect(goldenFlow).not.toContain(".catch(() => {})");
  });

  it("keeps the Phase 0 acceptance runner isolated, loopback-only, and debuggable", async () => {
    const harness = await studioFile("scripts/phase0-e2e.sh");
    expect(harness).toContain("pnpm --filter @spool/studio build");
    expect(harness).toContain("next start --hostname 127.0.0.1");
    expect(harness).toContain("TROVE_DOWNLOAD_DIR=");
    expect(harness).toContain("SPOOL_OFFLINE=0");
    expect(harness).toContain("SPOOL_LLM_PROVIDER=none");
    expect(harness).toContain("SPOOL_LLM_EGRESS_CONSENT=0");
    expect(harness).toContain("SPOOL_WATCH_INTERVAL=0");
    expect(harness).toContain("require_command ffprobe");
    expect(harness).toContain("curl --noproxy '*'");
    expect(harness).toContain('NO_PROXY="$LOOPBACK_NO_PROXY"');
    expect(harness).toContain('no_proxy="$LOOPBACK_NO_PROXY"');
    const unsafeCurlCalls = harness
      .split("\n")
      .filter((line) => /^\s*curl\s/u.test(line) && !line.includes("--noproxy '*'"));
    expect(unsafeCurlCalls).toEqual([]);
    expect(harness).toContain("--connect-timeout");
    expect(harness).toContain("--max-time");
    expect(harness).toContain("stop_process_tree");
    expect(harness).toContain("wait_for_tree_exit");
    expect(harness).toContain("kill -KILL");
    expect(harness).toContain("SPOOL_PHASE0_E2E_TEARDOWN_PROBE");
    expect(harness).toContain("preserved failure artifacts");
    expect(harness).toContain("HARNESS_SUCCEEDED=0");
    expect(harness).toContain("HARNESS_SUCCEEDED=1");
  });

  it("bounds teardown by escalating a TERM-ignoring process to KILL", async () => {
    const { stdout } = await execFileAsync("bash", ["scripts/phase0-e2e.sh"], {
      cwd: process.cwd(),
      env: { ...process.env, SPOOL_PHASE0_E2E_TEARDOWN_PROBE: "1" },
      timeout: 10_000,
    });
    expect(stdout).toContain("Phase 0 bounded teardown probe passed");
  }, 15_000);
});
