import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DELETE,
  dynamic,
  GET,
  HEAD,
  OPTIONS,
  PATCH,
  POST,
  PUT,
  runtime,
} from "@/app/api/engine/[...path]/route";
import { forwardEngineRequest } from "@/lib/engine-proxy";

type ProxyEnv = Readonly<Record<string, string | undefined>>;
type FetchInit = RequestInit & { duplex?: "half" };

const studioUrl = "http://127.0.0.1:3000/api/engine/api/v1/jobs";
const defaultEnv: ProxyEnv = {
  SPOOL_ENGINE_URL: "http://127.0.0.1:8899",
  SPOOL_ENGINE_TOKEN: "server-secret",
};

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function request(
  url = studioUrl,
  init: RequestInit & { duplex?: "half" } = {},
): Request {
  return new Request(url, init);
}

function fetchReturning(response: Response) {
  const calls: Array<{ url: string; init: FetchInit | undefined }> = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init: init as FetchInit | undefined });
    return response;
  }) as typeof fetch;
  return { fetchImpl, calls };
}

async function expectError(response: Response, status: number, error: string) {
  expect(response.status).toBe(status);
  expect(response.headers.get("cache-control")).toBe("no-store");
  await expect(response.json()).resolves.toEqual({ error });
}

describe("engine proxy upstream construction", () => {
  it("uses only the configured engine origin, encoded path, and incoming query", async () => {
    const upstream = fetchReturning(new Response("ok"));
    await forwardEngineRequest(
      request(`${studioUrl}?tag=a&tag=b&name=hello%20world`),
      ["api", "v1", "sources", "source id"],
      {
        env: { SPOOL_ENGINE_URL: "https://127.42.7.9:9443/" },
        fetchImpl: upstream.fetchImpl,
      },
    );

    expect(upstream.calls).toHaveLength(1);
    expect(upstream.calls[0]!.url).toBe(
      "https://127.42.7.9:9443/api/v1/sources/source%20id?tag=a&tag=b&name=hello%20world",
    );
  });

  it("defaults to the loopback engine origin", async () => {
    const upstream = fetchReturning(new Response("ok"));
    await forwardEngineRequest(request(), ["api", "v1", "health"], {
      env: {},
      fetchImpl: upstream.fetchImpl,
    });
    expect(upstream.calls[0]!.url).toBe("http://127.0.0.1:8899/api/v1/health");
  });

  it.each([
    "ftp://127.0.0.1:8899",
    "http://user:password@127.0.0.1:8899",
    "http://127.0.0.1:8899/api/v1",
    "http://127.0.0.1:8899/?query=yes",
    "http://127.0.0.1:8899/#fragment",
    "not a URL",
  ])("rejects unsafe engine configuration %s before fetching", async (engineUrl) => {
    const upstream = vi.fn();
    const response = await forwardEngineRequest(request(), ["api", "v1", "jobs"], {
      env: { SPOOL_ENGINE_URL: engineUrl, SPOOL_ENGINE_TOKEN: "do-not-leak" },
      fetchImpl: upstream as unknown as typeof fetch,
    });
    await expectError(response, 500, "engine_proxy_misconfigured");
    expect(upstream).not.toHaveBeenCalled();
  });
});

describe("engine proxy request boundary", () => {
  it("injects only the configured bearer and never trusts browser credentials", async () => {
    const upstream = fetchReturning(new Response("ok", {
      headers: { Authorization: "Bearer reflected", "Proxy-Authorization": "Basic reflected" },
    }));
    const response = await forwardEngineRequest(
      request(studioUrl, {
        headers: {
          Accept: "application/json",
          Authorization: "Bearer browser-token",
          Cookie: "session=browser-cookie",
          Host: "evil.example",
          Origin: "http://127.0.0.1:3000",
        },
      }),
      ["api", "v1", "jobs"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );

    const headers = new Headers(upstream.calls[0]!.init?.headers);
    expect(headers.get("authorization")).toBe("Bearer server-secret");
    expect(headers.has("cookie")).toBe(false);
    expect(headers.has("host")).toBe(false);
    expect(headers.has("origin")).toBe(false);
    expect(response.headers.has("authorization")).toBe(false);
    expect(response.headers.has("proxy-authorization")).toBe(false);
  });

  it("treats a blank token as unauthenticated loopback development", async () => {
    const upstream = fetchReturning(new Response("ok"));
    await forwardEngineRequest(request(), ["api", "v1", "health"], {
      env: { SPOOL_ENGINE_TOKEN: "  " },
      fetchImpl: upstream.fetchImpl,
    });
    expect(new Headers(upstream.calls[0]!.init?.headers).has("authorization")).toBe(false);
  });

  it.each(["line-one\nline-two", "emoji-😀", "nul\u0000token"])(
    "returns a structured configuration error for an invalid bearer %j",
    async (token) => {
      const upstream = vi.fn();
      const response = await forwardEngineRequest(request(), ["api", "v1", "health"], {
        env: { SPOOL_ENGINE_TOKEN: token },
        fetchImpl: upstream as unknown as typeof fetch,
      });
      await expectError(response, 500, "engine_proxy_misconfigured");
      expect(upstream).not.toHaveBeenCalled();
    },
  );

  it("forwards the explicit request allowlist and forces identity encoding", async () => {
    const upstream = fetchReturning(new Response("ok"));
    await forwardEngineRequest(
      request(studioUrl, {
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
          Range: "bytes=0-99",
          "If-Range": "etag-a",
          "If-Match": "etag-b",
          "If-None-Match": "etag-c",
          "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT",
          "If-Unmodified-Since": "Wed, 21 Oct 2015 07:28:00 GMT",
          "Idempotency-Key": "once",
          "Last-Event-ID": "event-7",
          "X-Not-Allowlisted": "private",
          "Accept-Encoding": "gzip",
        },
      }),
      ["api", "v1", "events"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );

    const headers = new Headers(upstream.calls[0]!.init?.headers);
    expect(Object.fromEntries(headers)).toMatchObject({
      accept: "text/event-stream",
      "accept-encoding": "identity",
      "content-type": "application/json",
      range: "bytes=0-99",
      "if-range": "etag-a",
      "if-match": "etag-b",
      "if-none-match": "etag-c",
      "if-modified-since": "Wed, 21 Oct 2015 07:28:00 GMT",
      "if-unmodified-since": "Wed, 21 Oct 2015 07:28:00 GMT",
      "idempotency-key": "once",
      "last-event-id": "event-7",
    });
    expect(headers.has("x-not-allowlisted")).toBe(false);
  });

  it("removes request headers nominated by Connection before adding server-owned headers", async () => {
    const upstream = fetchReturning(new Response("ok"));
    await forwardEngineRequest(
      request(studioUrl, {
        headers: {
          Connection: "Range, Authorization, X-Trace",
          Range: "bytes=0-9",
          Authorization: "Bearer browser",
          "X-Trace": "hidden",
        },
      }),
      ["api", "v1", "media"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );

    const headers = new Headers(upstream.calls[0]!.init?.headers);
    expect(headers.has("range")).toBe(false);
    expect(headers.has("x-trace")).toBe(false);
    expect(headers.get("authorization")).toBe("Bearer server-secret");
    expect(headers.has("connection")).toBe(false);
  });

  it("ignores malformed Connection nominations without throwing", async () => {
    const upstream = fetchReturning(new Response("ok"));
    const response = await forwardEngineRequest(
      request(studioUrl, { headers: { Connection: "valid-token, bad@token" } }),
      ["api", "v1", "jobs"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );
    expect(response.status).toBe(200);
    expect(upstream.calls).toHaveLength(1);
  });

  it("preserves mutation method, body stream, signal, and Node duplex", async () => {
    const upstream = fetchReturning(new Response("created", { status: 201 }));
    const req = request(studioUrl, {
      method: "POST",
      headers: {
        Origin: "http://127.0.0.1:3000",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ url: "https://example.com/video" }),
    });
    const originalBody = req.body;
    const response = await forwardEngineRequest(req, ["api", "v1", "jobs"], {
      env: defaultEnv,
      fetchImpl: upstream.fetchImpl,
    });

    expect(response.status).toBe(201);
    expect(upstream.calls[0]!.init?.method).toBe("POST");
    expect(upstream.calls[0]!.init?.body).toBe(originalBody);
    expect(upstream.calls[0]!.init?.signal).toBe(req.signal);
    expect(upstream.calls[0]!.init?.duplex).toBe("half");
    expect(upstream.calls[0]!.init?.redirect).toBe("manual");
    expect(upstream.calls[0]!.init?.cache).toBe("no-store");
  });

  it("does not add duplex when the incoming request has no body", async () => {
    const upstream = fetchReturning(new Response("ok"));
    await forwardEngineRequest(request(), ["api", "v1", "jobs"], {
      env: defaultEnv,
      fetchImpl: upstream.fetchImpl,
    });
    expect(upstream.calls[0]!.init?.body).toBeUndefined();
    expect(upstream.calls[0]!.init?.duplex).toBeUndefined();
  });
});

describe("engine proxy origin policy", () => {
  it.each(["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])(
    "requires an exact present Origin for %s",
    async (method) => {
      const upstream = vi.fn();
      const response = await forwardEngineRequest(
        request(studioUrl, { method }),
        ["api", "v1", "jobs"],
        { env: defaultEnv, fetchImpl: upstream as unknown as typeof fetch },
      );
      await expectError(response, 403, "origin_forbidden");
      expect(upstream).not.toHaveBeenCalled();
    },
  );

  it.each(["GET", "HEAD"])("allows originless loopback %s reads", async (method) => {
    const upstream = fetchReturning(new Response(null, { status: 200 }));
    const response = await forwardEngineRequest(
      request(studioUrl, { method }),
      ["api", "v1", "jobs"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );
    expect(response.status).toBe(200);
    expect(upstream.calls).toHaveLength(1);
  });

  it.each(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])(
    "rejects a public Next request URL for %s before token injection",
    async (method) => {
      const upstream = vi.fn();
      const headers = method === "GET" || method === "HEAD"
        ? undefined
        : { Origin: "https://studio.example" };
      const response = await forwardEngineRequest(
        request("https://studio.example/api/engine/api/v1/jobs", { method, headers }),
        ["api", "v1", "jobs"],
        { env: defaultEnv, fetchImpl: upstream as unknown as typeof fetch },
      );
      await expectError(response, 403, "origin_forbidden");
      expect(upstream).not.toHaveBeenCalled();
    },
  );

  it.each([
    "",
    "null",
    "https://evil.example",
    "http://127.0.0.1:3001",
    "http://localhost:3000",
    "http://127.0.0.1:3000/path",
    "not an origin",
  ])("rejects present non-exact Origin %j", async (origin) => {
    const upstream = vi.fn();
    const response = await forwardEngineRequest(
      request(studioUrl, { method: "POST", headers: { Origin: origin } }),
      ["api", "v1", "jobs"],
      { env: defaultEnv, fetchImpl: upstream as unknown as typeof fetch },
    );
    await expectError(response, 403, "origin_forbidden");
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each([
    ["http://localhost:3000/api/engine/api/v1/jobs", "http://localhost:3000"],
    ["http://127.42.7.9:3000/api/engine/api/v1/jobs", "http://127.42.7.9:3000"],
    ["http://[::1]:3000/api/engine/api/v1/jobs", "http://[::1]:3000"],
  ])("allows exact loopback mutation origin %s", async (url, origin) => {
    const upstream = fetchReturning(new Response("ok"));
    const response = await forwardEngineRequest(
      request(url, { method: "POST", headers: { Origin: origin } }),
      ["api", "v1", "jobs"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );
    expect(response.status).toBe(200);
    expect(upstream.calls).toHaveLength(1);
  });

  it.each(["cross-site", "same-site"])(
    "rejects Sec-Fetch-Site: %s even for an originless read",
    async (site) => {
      const upstream = vi.fn();
      const response = await forwardEngineRequest(
        request(studioUrl, { headers: { "Sec-Fetch-Site": site } }),
        ["api", "v1", "jobs"],
        { env: defaultEnv, fetchImpl: upstream as unknown as typeof fetch },
      );
      await expectError(response, 403, "origin_forbidden");
      expect(upstream).not.toHaveBeenCalled();
    },
  );
});

describe("engine proxy path confinement", () => {
  it.each([
    { path: [] as string[] },
    { path: ["api", "v1", ""] },
    { path: ["api", "v1", "."] },
    { path: ["api", "v1", ".."] },
    { path: ["api", "v1", "source/id"] },
    { path: ["api", "v1", "source\\id"] },
    { path: ["api", "v1", "//evil.example"] },
    { path: ["api", "v1", "https://evil.example"] },
    { path: ["api", "v1", "bad\u0000id"] },
    { path: ["api", "v1", "bad\u007fid"] },
    { path: ["not-api", "v1", "jobs"] },
  ])("rejects invalid decoded path $path before fetching", async ({ path }) => {
    const upstream = vi.fn();
    const response = await forwardEngineRequest(request(), path, {
      env: defaultEnv,
      fetchImpl: upstream as unknown as typeof fetch,
    });
    await expectError(response, 400, "invalid_engine_path");
    expect(upstream).not.toHaveBeenCalled();
  });

  it("rejects a lone surrogate instead of throwing during encoding", async () => {
    const upstream = vi.fn();
    const response = await forwardEngineRequest(request(), ["api", "v1", "\ud800"], {
      env: defaultEnv,
      fetchImpl: upstream as unknown as typeof fetch,
    });
    await expectError(response, 400, "invalid_engine_path");
    expect(upstream).not.toHaveBeenCalled();
  });

  it("does not recursively decode a literal percent-encoded parameter", async () => {
    const upstream = fetchReturning(new Response("ok"));
    await forwardEngineRequest(request(), ["api", "v1", "sources", "%2f"], {
      env: defaultEnv,
      fetchImpl: upstream.fetchImpl,
    });
    expect(upstream.calls[0]!.url).toBe("http://127.0.0.1:8899/api/v1/sources/%252f");
  });
});

describe("engine proxy streaming response", () => {
  it("returns SSE incrementally without waiting for the stream to close", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(value) {
        controller = value;
      },
    });
    const upstream = fetchReturning(new Response(body, {
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    }));

    const response = await forwardEngineRequest(request(), ["api", "v1", "events"], {
      env: defaultEnv,
      fetchImpl: upstream.fetchImpl,
    });
    const reader = response.body!.getReader();
    controller.enqueue(new TextEncoder().encode("data: first\n\n"));
    const first = await reader.read();
    expect(new TextDecoder().decode(first.value)).toBe("data: first\n\n");
    expect(first.done).toBe(false);
    controller.close();
  });

  it("preserves ranged media bytes and representation headers", async () => {
    const bytes = new Uint8Array([0, 1, 2, 3]);
    const upstream = fetchReturning(new Response(bytes, {
      status: 206,
      headers: {
        "Content-Type": "video/mp4",
        "Content-Length": "4",
        "Content-Range": "bytes 0-3/100",
        "Accept-Ranges": "bytes",
        "Content-Disposition": "attachment; filename=clip.mp4",
        ETag: '"clip-v1"',
        "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
      },
    }));
    const response = await forwardEngineRequest(
      request(studioUrl, { headers: { Range: "bytes=0-3" } }),
      ["api", "v1", "clips", "clip-1", "file"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );

    expect(response.status).toBe(206);
    await expect(response.arrayBuffer()).resolves.toEqual(bytes.buffer);
    expect(response.headers.get("content-type")).toBe("video/mp4");
    expect(response.headers.get("content-length")).toBe("4");
    expect(response.headers.get("content-range")).toBe("bytes 0-3/100");
    expect(response.headers.get("accept-ranges")).toBe("bytes");
    expect(response.headers.get("content-disposition")).toContain("clip.mp4");
    expect(response.headers.get("etag")).toBe('"clip-v1"');
    expect(response.headers.get("last-modified")).toBe("Wed, 21 Oct 2015 07:28:00 GMT");
  });

  it("preserves a 416 Content-Range response", async () => {
    const upstream = fetchReturning(new Response("range", {
      status: 416,
      headers: { "Content-Range": "bytes */100" },
    }));
    const response = await forwardEngineRequest(request(), ["api", "v1", "media"], {
      env: defaultEnv,
      fetchImpl: upstream.fetchImpl,
    });
    expect(response.status).toBe(416);
    expect(response.headers.get("content-range")).toBe("bytes */100");
  });

  it("strips fixed and dynamic hop-by-hop, cookies, and CORS response headers", async () => {
    const upstream = fetchReturning(new Response("ok", {
      headers: {
        Connection: "X-Internal, Keep-Alive",
        "X-Internal": "private",
        "Keep-Alive": "timeout=5",
        "Proxy-Connection": "close",
        "Set-Cookie": "session=engine",
        "Set-Cookie2": "legacy=engine",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Authorization",
        "X-Safe": "visible",
      },
    }));
    const response = await forwardEngineRequest(request(), ["api", "v1", "jobs"], {
      env: defaultEnv,
      fetchImpl: upstream.fetchImpl,
    });
    expect(Object.fromEntries(response.headers)).toEqual({
      "content-type": "text/plain;charset=UTF-8",
      "x-safe": "visible",
    });
  });

  it.each(["HEAD", "GET"])("emits a null body for %s when required", async (method) => {
    const status = method === "HEAD" ? 200 : 304;
    const upstream = fetchReturning(new Response(method === "HEAD" ? "hidden" : null, { status }));
    const response = await forwardEngineRequest(
      request(studioUrl, { method }),
      ["api", "v1", "jobs"],
      { env: defaultEnv, fetchImpl: upstream.fetchImpl },
    );
    expect(response.status).toBe(status);
    expect(response.body).toBeNull();
  });
});

describe("engine proxy redirects and failures", () => {
  it("rewrites a same-engine API redirect to the same-origin proxy without following it", async () => {
    const upstream = fetchReturning(new Response(null, {
      status: 307,
      headers: { Location: "/api/v1/jobs/job-1?view=full#state" },
    }));
    const response = await forwardEngineRequest(request(), ["api", "v1", "jobs"], {
      env: defaultEnv,
      fetchImpl: upstream.fetchImpl,
    });
    expect(upstream.calls).toHaveLength(1);
    expect(upstream.calls[0]!.init?.redirect).toBe("manual");
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe(
      "/api/engine/api/v1/jobs/job-1?view=full#state",
    );
  });

  it.each([
    "https://evil.example/steal",
    "/admin",
    "http://[not-an-ipv6-address",
  ])("blocks unsafe upstream redirect %s", async (location) => {
    const cancel = vi.fn();
    const body = new ReadableStream({ cancel });
    const fetchImpl = vi.fn(async () => new Response(body, {
      status: 302,
      headers: { Location: location },
    }));
    const response = await forwardEngineRequest(request(), ["api", "v1", "jobs"], {
      env: defaultEnv,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await expectError(response, 502, "engine_redirect_forbidden");
    expect(response.headers.has("location")).toBe(false);
    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("returns a non-secret structured error when the engine is unreachable", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error("connect ECONNREFUSED secret-host");
    });
    const response = await forwardEngineRequest(request(), ["api", "v1", "jobs"], {
      env: defaultEnv,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await expectError(response, 502, "engine_unreachable");
  });
});

describe("Next engine route adapters", () => {
  const adapters = { GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS } as const;

  it("pins the route to the Node runtime and dynamic rendering", () => {
    expect(runtime).toBe("nodejs");
    expect(dynamic).toBe("force-dynamic");
  });

  it.each(Object.entries(adapters))(
    "%s awaits catch-all params and delegates with the original method",
    async (method, adapter) => {
      vi.stubEnv("SPOOL_ENGINE_URL", "http://127.0.0.1:8899");
      vi.stubEnv("SPOOL_ENGINE_TOKEN", "");
      const fetchImpl = vi.fn(async (...args: Parameters<typeof fetch>) => {
        void args;
        return new Response("upstream-body");
      });
      vi.stubGlobal("fetch", fetchImpl);

      const response = await adapter(
        request(studioUrl, {
          method,
          headers: method === "GET" || method === "HEAD"
            ? undefined
            : { Origin: "http://127.0.0.1:3000" },
        }),
        { params: Promise.resolve({ path: ["api", "v1", "jobs"] }) },
      );

      expect(fetchImpl).toHaveBeenCalledOnce();
      expect(fetchImpl.mock.calls[0]![0]).toBe("http://127.0.0.1:8899/api/v1/jobs");
      expect(fetchImpl.mock.calls[0]![1]?.method).toBe(method);
      if (method === "HEAD") expect(response.body).toBeNull();
      else expect(await response.text()).toBe("upstream-body");
    },
  );
});
