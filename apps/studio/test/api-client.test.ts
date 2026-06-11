import { describe, it, expect, vi } from "vitest";
import { SpoolApiClient, SpoolApiError } from "@spool/api-client";

/* The typed REST client mirrors the engine's api_v1. Here we inject a fake fetch and assert the
 * wire shape (path / method / body) — the contract the studio relies on. */

function clientWithSpy() {
  const calls: { url: string; method?: string; body?: unknown }[] = [];
  const fakeFetch = (async (url: unknown, init: { method?: string; body?: string } = {}) => {
    calls.push({ url: String(url), method: init.method, body: init.body ? JSON.parse(init.body) : undefined });
    return {
      ok: true,
      status: 201,
      text: async () => JSON.stringify({ id: "job1", kind: "produce", status: "queued" }),
      json: async () => ({}),
    };
  }) as unknown as typeof globalThis.fetch;
  return { client: new SpoolApiClient({ baseUrl: "http://x", fetch: fakeFetch }), calls };
}

describe("SpoolApiClient.produce", () => {
  it("POSTs a saved recipe by id to /sources/<id>/produce", async () => {
    const { client, calls } = clientWithSpy();
    const r = await client.produce("src9", { recipe_id: "r1" });
    expect(r).toMatchObject({ id: "job1", kind: "produce" });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("http://x/api/v1/sources/src9/produce");
    expect(calls[0]!.method).toBe("POST");
    expect(calls[0]!.body).toEqual({ recipe_id: "r1" });
  });

  it("POSTs an inline recipe (no id) for an unsaved recipe — all settings ride along", async () => {
    const { client, calls } = clientWithSpy();
    await client.produce("src9", {
      content_mode: "funny", count: 3, aspect: "1:1", reframe_mode: "center",
      caption_preset: "minimal", platform: "reels", fast: true, brand_kit_id: "kitX",
      weights: { hook: 5 },
    });
    expect(calls[0]!.body).toEqual({
      content_mode: "funny", count: 3, aspect: "1:1", reframe_mode: "center",
      caption_preset: "minimal", platform: "reels", fast: true, brand_kit_id: "kitX",
      weights: { hook: 5 },
    });
  });
});

// ── SSE clean-close reconnect (HIGH finding 3.4) ─────────────────────────────
// A clean EOF (engine restart, idle proxy timeout) must route through onError so
// callers (EngineProvider) can reconnect with backoff — not silently freeze.

function sseResponse(frames: string[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      const enc = new TextEncoder();
      for (const f of frames) c.enqueue(enc.encode(`data: ${f}\n\n`));
      c.close(); // clean EOF — the engine restarted / proxy idled out
    },
  });
  return new Response(stream, { status: 200 });
}

describe("subscribeEvents clean close", () => {
  it("routes a clean EOF through onError so callers reconnect", async () => {
    const fetchImpl = vi.fn(async () => sseResponse(['{"ts":1,"jobs":[],"transcripts":[],"clips":[]}']));
    const client = new SpoolApiClient({ baseUrl: "http://x", fetch: fetchImpl as unknown as typeof fetch });
    const snaps: unknown[] = [];
    const errors: unknown[] = [];
    client.subscribeEvents((s) => snaps.push(s), { onError: (e) => errors.push(e) });
    await vi.waitFor(() => expect(errors.length).toBe(1));
    expect(snaps.length).toBe(1);
    expect((errors[0] as SpoolApiError).code).toBe("events_closed");
  });

  it("does NOT fire onError when the caller unsubscribed", async () => {
    const fetchImpl = vi.fn(async () => sseResponse([]));
    const client = new SpoolApiClient({ baseUrl: "http://x", fetch: fetchImpl as unknown as typeof fetch });
    const errors: unknown[] = [];
    const stop = client.subscribeEvents(() => {}, { onError: (e) => errors.push(e) });
    stop();
    await new Promise((r) => setTimeout(r, 20));
    expect(errors.length).toBe(0);
  });
});
