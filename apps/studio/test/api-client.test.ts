import { describe, it, expect } from "vitest";
import { SpoolApiClient } from "@spool/api-client";

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
    expect(calls[0].url).toBe("http://x/api/v1/sources/src9/produce");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({ recipe_id: "r1" });
  });

  it("POSTs an inline recipe (no id) for an unsaved recipe — all settings ride along", async () => {
    const { client, calls } = clientWithSpy();
    await client.produce("src9", {
      content_mode: "funny", count: 3, aspect: "1:1", reframe_mode: "center",
      caption_preset: "minimal", platform: "reels", fast: true, brand_kit_id: "kitX",
      weights: { hook: 5 },
    });
    expect(calls[0].body).toEqual({
      content_mode: "funny", count: 3, aspect: "1:1", reframe_mode: "center",
      caption_preset: "minimal", platform: "reels", fast: true, brand_kit_id: "kitX",
      weights: { hook: 5 },
    });
  });
});
