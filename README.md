# Spool

**Local-first, open-source clip studio** — turn any long video into platform-ready vertical
clips on your machine, with editor-grade manual control and a gated agent surface. No per-clip
credits, no uploads.

Spool pairs a deterministic local engine with the supported Phase 0 workflow:

```
download/import → transcribe → select a transcript range manually → cut → edit/reframe/caption → render/export
```

Remote reasoning, automated discovery, and watch reconciliation are unavailable in Phase 0.
Those paths fail closed; there is no active Codex or other remote reasoning provider. Local
transcript selection, cutting, editing, reframing, captioning, and rendering remain available.

Spool exposes that workflow through two local clients over **one** JSON API:

- a **Next.js studio** — manual, editor-grade control over the authenticated REST API, and
- a **Python FastMCP stdio server** — read-only inspection for Claude Desktop/Code, Cursor,
  and other MCP clients during Phase 0.

> **Phase 0 safety boundary:** both clients read the same engine, job store, and files, but
> agent mutations fail closed with `agent_mutation_disabled`. Mutation parity remains future
> work until the Phase 4 approval and undo contract ships.

The defensible bit: reframe follows the active speaker with **no face-detection model** —
it fuses audio diarization (who's talking) with cheap ffmpeg ROI motion (where each face is),
so the vertical crop pans to the speaker offline, in seconds. (`engine/clip/reframe.py`.)

## Monorepo layout

| Path | What |
|---|---|
| `engine/` | Python engine: Flask JSON API plus the working FastMCP stdio server (`mcp_server.py`), job system, downloader, transcription, and diarization. |
| `engine/clip/` | The clip back-half — moment-finding, cut, reframe (diar⊕ROI pan), captions, export. |
| `apps/studio/` | The Next.js + TypeScript + Tailwind + shadcn studio UI. |
| `packages/types/` | Shared TS types mirroring the engine data model. |
| `packages/api-client/` | Typed REST client for the engine's JSON API. |
| `packages/ui/` | Design-system components — the studio's building blocks. |
| `docs/` | Engineering spec, product overview, and the approved visual design. |

## Develop

Prerequisites: **Node 20+**, **pnpm**, **Python 3.11+**, and **ffmpeg**. (yt-dlp is pinned to
master and installed by the engine setup.)

```bash
# JS workspace (studio + packages)
pnpm install
pnpm dev

# engine (Python) — headless JSON API on http://127.0.0.1:8899
cd engine && ./trove.sh
# …or build the container:
docker build -t spool-engine engine/
```

The Studio's `/api/engine` route injects the optional server-side engine token for JSON, SSE,
media, and downloads. Copy `apps/studio/.env.example` to `.env.local` when token auth is enabled.
Both Studio scripts intentionally bind to `127.0.0.1`: this bearer-injecting route is
**loopback-only** and must not be exposed by a public reverse proxy.

The build follows the phased roadmap in [`docs/Spool_Engineering-Spec.md`](docs/Spool_Engineering-Spec.md)
(§5). A root `docker compose` and the headless de-coupling from the legacy editor land in Phase 0.

## License & credits

Spool is licensed under **Apache-2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

The clip back-half primitives in `engine/clip/backhalf/` are adapted from an MIT-licensed
project by **Louise de Sadeleer**; the engine foundation is first-party, inspired by `reclip`
(MIT). Full texts: [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).
