const DEFAULT_ENGINE_URL = "http://127.0.0.1:8899";

const REQUEST_HEADER_ALLOWLIST = [
  "accept",
  "content-type",
  "range",
  "if-range",
  "if-match",
  "if-none-match",
  "if-modified-since",
  "if-unmodified-since",
  "idempotency-key",
  "last-event-id",
] as const;

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "proxy-connection",
]);

const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const BODYLESS_STATUSES = new Set([204, 205, 304]);
const CONNECTION_TOKEN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/u;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/u;

type ProxyEnvironment = Readonly<Record<string, string | undefined>>;

type ForwardEngineDependencies = {
  env?: ProxyEnvironment;
  fetchImpl?: typeof globalThis.fetch;
};

type NodeRequestInit = RequestInit & { duplex?: "half" };

function errorResponse(error: string, status: number): Response {
  return new Response(JSON.stringify({ error }), {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase();
  if (normalized === "localhost" || normalized === "[::1]") return true;

  const octets = normalized.split(".");
  if (octets.length !== 4 || octets[0] !== "127") return false;
  return octets.every((octet) => /^\d{1,3}$/u.test(octet) && Number(octet) <= 255);
}

function isHttpUrl(url: URL): boolean {
  return url.protocol === "http:" || url.protocol === "https:";
}

function parseIncomingUrl(request: Request): URL | null {
  try {
    const url = new URL(request.url);
    if (!isHttpUrl(url) || url.username || url.password || !isLoopbackHostname(url.hostname)) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

function hasAllowedOrigin(request: Request, requestUrl: URL): boolean {
  const fetchSite = request.headers.get("sec-fetch-site")?.trim().toLowerCase();
  if (fetchSite === "cross-site" || fetchSite === "same-site") return false;

  if (!request.headers.has("origin")) {
    return request.method === "GET" || request.method === "HEAD";
  }

  const value = request.headers.get("origin") ?? "";
  try {
    const origin = new URL(value);
    return (
      isHttpUrl(origin) &&
      !origin.username &&
      !origin.password &&
      isLoopbackHostname(origin.hostname) &&
      origin.pathname === "/" &&
      !origin.search &&
      !origin.hash &&
      origin.origin === requestUrl.origin
    );
  } catch {
    return false;
  }
}

function encodeEnginePath(path: readonly string[]): string[] | null {
  if (!Array.isArray(path) || path.length < 2 || path[0] !== "api" || path[1] !== "v1") {
    return null;
  }

  const encoded: string[] = [];
  for (const segment of path) {
    if (
      typeof segment !== "string" ||
      !segment ||
      segment === "." ||
      segment === ".." ||
      segment.includes("/") ||
      segment.includes("\\") ||
      CONTROL_CHARACTER.test(segment)
    ) {
      return null;
    }
    try {
      encoded.push(encodeURIComponent(segment));
    } catch {
      return null;
    }
  }
  return encoded;
}

function parseEngineOrigin(value: string | undefined): URL | null {
  try {
    const url = new URL(value?.trim() || DEFAULT_ENGINE_URL);
    if (
      !isHttpUrl(url) ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash
    ) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

function connectionNominations(headers: Headers): Set<string> {
  const nominated = new Set<string>();
  const value = headers.get("connection");
  if (!value) return nominated;

  for (const part of value.split(",")) {
    const token = part.trim();
    if (CONNECTION_TOKEN.test(token)) nominated.add(token.toLowerCase());
  }
  return nominated;
}

function upstreamHeaders(request: Request, token: string | undefined): Headers {
  const headers = new Headers();
  const nominated = connectionNominations(request.headers);

  for (const name of REQUEST_HEADER_ALLOWLIST) {
    if (HOP_BY_HOP_HEADERS.has(name) || nominated.has(name)) continue;
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }

  // Node fetch transparently decompresses responses while retaining the original
  // Content-Encoding/Content-Length. Identity keeps streamed media metadata truthful.
  headers.set("accept-encoding", "identity");
  const bearer = token?.trim();
  if (bearer) headers.set("authorization", `Bearer ${bearer}`);
  return headers;
}

function downstreamHeaders(upstream: Response): Headers {
  const headers = new Headers();
  const nominated = connectionNominations(upstream.headers);

  for (const [name, value] of upstream.headers) {
    const normalized = name.toLowerCase();
    if (
      HOP_BY_HOP_HEADERS.has(normalized) ||
      nominated.has(normalized) ||
      normalized === "set-cookie" ||
      normalized === "set-cookie2" ||
      normalized === "authorization" ||
      normalized.startsWith("access-control-")
    ) {
      continue;
    }
    headers.append(name, value);
  }
  return headers;
}

function rewriteRedirect(
  upstream: Response,
  upstreamUrl: URL,
  engineOrigin: URL,
  headers: Headers,
): Response | null {
  if (!REDIRECT_STATUSES.has(upstream.status)) return null;

  const location = upstream.headers.get("location");
  if (!location) return errorResponse("engine_redirect_forbidden", 502);

  try {
    const target = new URL(location, upstreamUrl);
    if (
      target.origin !== engineOrigin.origin ||
      (target.pathname !== "/api/v1" && !target.pathname.startsWith("/api/v1/"))
    ) {
      return errorResponse("engine_redirect_forbidden", 502);
    }
    headers.set(
      "location",
      `/api/engine${target.pathname}${target.search}${target.hash}`,
    );
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers,
    });
  } catch {
    return errorResponse("engine_redirect_forbidden", 502);
  }
}

export async function forwardEngineRequest(
  request: Request,
  path: readonly string[],
  dependencies: ForwardEngineDependencies = {},
): Promise<Response> {
  const requestUrl = parseIncomingUrl(request);
  if (!requestUrl || !hasAllowedOrigin(request, requestUrl)) {
    return errorResponse("origin_forbidden", 403);
  }

  const encodedPath = encodeEnginePath(path);
  if (!encodedPath) return errorResponse("invalid_engine_path", 400);

  const env = dependencies.env ?? process.env;
  const engineOrigin = parseEngineOrigin(env.SPOOL_ENGINE_URL);
  if (!engineOrigin) return errorResponse("engine_proxy_misconfigured", 500);

  const upstreamUrl = new URL(`/${encodedPath.join("/")}`, engineOrigin);
  upstreamUrl.search = requestUrl.search;
  if (upstreamUrl.origin !== engineOrigin.origin) {
    return errorResponse("invalid_engine_path", 400);
  }

  const init: NodeRequestInit = {
    method: request.method,
    headers: upstreamHeaders(request, env.SPOOL_ENGINE_TOKEN),
    redirect: "manual",
    cache: "no-store",
    credentials: "omit",
    signal: request.signal,
  };
  if (request.body !== null) {
    init.body = request.body;
    init.duplex = "half";
  }

  let upstream: Response;
  try {
    const fetchImpl = dependencies.fetchImpl ?? globalThis.fetch.bind(globalThis);
    upstream = await fetchImpl(upstreamUrl.href, init);
  } catch {
    return errorResponse("engine_unreachable", 502);
  }

  const headers = downstreamHeaders(upstream);
  const redirect = rewriteRedirect(upstream, upstreamUrl, engineOrigin, headers);
  if (redirect) return redirect;

  const body = request.method === "HEAD" || BODYLESS_STATUSES.has(upstream.status)
    ? null
    : upstream.body;
  return new Response(body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  });
}
