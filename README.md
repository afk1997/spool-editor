# Spool

**Local-first, open-source clip studio** — turn any long video into platform-ready vertical
clips, driven by an agent, running entirely on your machine. No per-clip credits, no uploads.

Spool pairs a deterministic offline engine

```
URL/file → download → transcribe → find moments → cut → reframe → caption → export
```

with two equal front-ends over **one** JSON API:

- a **Next.js studio** — manual, editor-grade control, and
- a **local MCP server** — agent mode (Claude Desktop/Code, Cursor, any MCP client).

> **Golden rule:** the UI and the MCP server are two clients of the same JSON API → same
> engine → same job store → same files on disk. Agent mode and manual mode never diverge.

The defensible bit: reframe follows the active speaker with **no face-detection model** —
it fuses audio diarization (who's talking) with cheap ffmpeg ROI motion (where each face is),
so the vertical crop pans to the speaker offline, in seconds. (`engine/clip/reframe.py`.)

## Monorepo layout

| Path | What |
|---|---|
| `engine/` | Python (Flask) engine: JSON API, MCP server, job system, downloader, transcription, diarization. |
| `engine/clip/` | The clip back-half — moment-finding, cut, reframe (diar⊕ROI pan), captions, export. |
| `apps/studio/` | The Next.js + TypeScript + Tailwind + shadcn studio UI. |
| `packages/types/` | Shared TS types mirroring the engine data model. |
| `packages/api-client/` | Typed REST client for the engine's JSON API. |
| `packages/mcp-client/` | TS client for the engine's MCP server. |
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

The build follows the phased roadmap in [`docs/Spool_Engineering-Spec.md`](docs/Spool_Engineering-Spec.md)
(§5). A root `docker compose` and the headless de-coupling from the legacy editor land in Phase 0.

## License & credits

Spool is licensed under **Apache-2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

The clip back-half primitives in `engine/clip/backhalf/` are adapted from an MIT-licensed
project by **Louise de Sadeleer**; the engine foundation is first-party, inspired by `reclip`
(MIT). Full texts: [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md).
