"use client";

import { createContext, useContext, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { SpoolApiClient } from "@spool/api-client";
import type { ClipJobView, EventsSnapshot, RankFactors, TranscriptWord } from "@spool/types";
import { useEngine, useEngineQuery, useLive } from "@/lib/engine-context";

/* The demo's `useSpool()` context, backed by the LIVE engine instead of mock data.
 * Maps the SSE snapshot into the demo's source/clip/job shapes so the ported demo
 * components render unchanged. `nav` drives Next routes; the agent loop calls the real
 * `/agent` endpoint and elicitation = the agent's `clarify` turn; "make clips" runs real
 * render pipelines. Per-source data (candidates, transcript) is mapped on demand by the
 * detail pages. Zero mock data (spec §6.2). */

export interface SpoolSource {
  id: string; title: string; src: string; dur: number; status: string;
  prog?: number; clips: number; kind: string; channel: string; res: string; fps: number;
  size: string; lang: string; added: string; scenes: number; transcriptId?: string; speakerCount: number;
}
export interface SpoolClip {
  id: string; title: string; src: string; dur: number; aspect: string; style: string;
  platform: string; status: string; prog?: number; tags?: string[]; renderId?: string; score?: number;
  start?: number; end?: number; // the cut window in source time, for slicing the transcript
}
export interface SpoolJob {
  id: string; type: string; label: string; src: string; status: string; prog: number; stage: string; eta: string; elapsed: string; err?: boolean;
  /** which engine surface owns this job — routes queue cancel/dismiss/retry actions */
  domain: "download" | "transcribe" | "clip";
}
export interface SpoolDep { id: string; name: string; note: string; status: string; ver: string }
export interface SpoolDownload { id: string; title: string; src: string; prog: number; status: string; size: string; speed: string; eta: string; err?: string | null }
export interface Toast { id: number; icon?: string; tone?: string; title: string; body?: string }

/** A discovery candidate, mapped from a `find_moments` job result. Glass-box = a real `score`
 *  that decomposes into named, reweightable `factors` (engine `moments.rank`), the effective
 *  `weights` used, plus the real named `signals` cues + a real transcript `excerpt`. */
export interface Candidate {
  id: string; title: string; start: number; end: number; mode: string;
  why: string; excerpt: string; signals: string[]; sel: boolean; source_id: string;
  score?: number; factors?: RankFactors; weights?: RankFactors;
}

/** The five glass-box ranking factors (engine snake_case keys), in display order. */
export const RANK_FACTORS = ["hook", "self_contained", "arc", "energy", "length_fit"] as const;

/** Client mirror of the engine's transparent score: round(100 · Σ(factorₖ·weightₖ) / Σweightₖ),
 *  factors in [0,1]. Identical math to `clip.moments.rank`, so the Discovery reweight slider stays
 *  instant (no server round-trip per tick, spec §6.4) yet equals what `POST /sources/<id>/rank`
 *  returns. Missing factors drop out of both sides; an all-zero weight vector scores 0. */
export function scoreFromFactors(factors: RankFactors = {}, weights: RankFactors = {}): number {
  const f = factors as Record<string, number | undefined>;
  const w = weights as Record<string, number | undefined>;
  const ks = RANK_FACTORS.filter((k) => f[k] != null);
  const tw = ks.reduce((a, k) => a + (w[k] ?? 0), 0);
  if (tw <= 0) return 0;
  return Math.round((100 * ks.reduce((a, k) => a + (f[k] ?? 0) * (w[k] ?? 0), 0)) / tw);
}

export interface AgentMessage {
  role: "agent" | "user" | "trace" | "working" | "elicit";
  id?: string; text?: string;
  tools?: { name: string; arg?: string; ms?: number }[];
  label?: string; icon?: string; prog?: number;
  kind?: "enum" | "multiselect" | "roi" | "confirm";
  tag?: string; q?: string; options?: { id: string; title: string; sub?: string; score?: number; def?: boolean }[] | string[]; yes?: string;
  answered?: boolean; answer?: unknown;
  jobChips?: { id: string; kind: string }[];
  /** source context the turn was asked with — re-sent when this elicit is answered */
  sourceId?: string;
}

const RECIPES = ["3 funny shorts", "Insightful carousel", "Hot-take TikToks", "Best moment → 9:16"];

const INITIAL_AGENT: AgentMessage[] = [
  { role: "agent", text: "Hi — I'm your clip agent. Paste a video URL or tell me what to make and I'll handle download, transcription, finding moments, reframing and captions. You decide at each step." },
];

function originOf(url: string | null | undefined): string {
  const u = (url || "").toLowerCase();
  if (u.includes("youtu")) return "youtube";
  if (u.includes("instagram")) return "instagram";
  if (u.includes("tiktok")) return "tiktok";
  if (u.includes("x.com") || u.includes("twitter")) return "x";
  return "file";
}
const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const ago = (sec: number) => {
  if (!sec || sec < 0) return "just now";
  const m = sec / 60, h = m / 60, d = h / 24;
  if (d >= 1) return `${Math.round(d)}d ago`;
  if (h >= 1) return `${Math.round(h)}h ago`;
  if (m >= 1) return `${Math.round(m)}m ago`;
  return "just now";
};
const human = (bytes: number) => {
  if (!bytes) return "—";
  const u = ["B", "KB", "MB", "GB"]; let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};

function mapSources(snap: EventsSnapshot | null): SpoolSource[] {
  if (!snap) return [];
  const clipCount = (sid: string) =>
    new Set(snap.clips.filter((c) => c.source_id === sid && c.clip_id).map((c) => c.clip_id)).size;
  return snap.jobs
    .filter((j) => j.status === "done" && j.filename)
    .map((j) => {
      const tj = snap.transcripts
        .filter((t) => t.parent_job_id === j.id)
        .sort((a, b) => b.elapsed_seconds - a.elapsed_seconds)[0];
      const status = tj?.status === "done" ? "ready" : tj && (tj.status === "running" || tj.status === "queued") ? "transcribing" : "no-candidates";
      const speakers = tj?.speaker_count ?? 0;
      return {
        id: j.id, title: j.title || j.url, src: originOf(j.url),
        dur: tj?.duration_seconds || 0, status, prog: tj?.progress_pct ?? 0,
        clips: clipCount(j.id),
        kind: speakers > 1 ? `podcast · ${speakers} speakers` : "talking-head",
        channel: originOf(j.url) === "file" ? "local file" : originOf(j.url),
        res: "—", fps: 30, size: human(j.total_bytes || j.downloaded_bytes),
        lang: tj?.language_detected || "—", added: ago(j.elapsed_seconds), scenes: 1,
        transcriptId: tj?.status === "done" ? tj.id : undefined, speakerCount: speakers,
      };
    })
    .reverse();
}

const PLAT_OF: Record<string, string> = { tiktok: "tiktok", reels: "reels", shorts: "shorts", linkedin: "linkedin", youtube: "youtube", x: "x" };

function clipTitle(jobs: ClipJobView[], cid: string): string {
  for (const j of jobs) {
    const t = (j.params?.title as string) || (j.result as { title?: string }).title;
    if (t) return t;
  }
  return "Clip " + cid.slice(0, 6);
}

function mapClips(snap: EventsSnapshot | null): SpoolClip[] {
  if (!snap) return [];
  const byClip = new Map<string, ClipJobView[]>();
  for (const c of snap.clips) {
    if (!c.clip_id) continue;
    const arr = byClip.get(c.clip_id) ?? [];
    arr.push(c);
    byClip.set(c.clip_id, arr);
  }
  const out: SpoolClip[] = [];
  for (const [cid, jobs] of byClip) {
    const cut = jobs.find((j) => j.kind === "cut") ?? jobs.find((j) => j.kind === "pipeline");
    const render = jobs.filter((j) => (j.kind === "export" || j.kind === "pipeline") && j.status === "done" && j.result.render_id).at(-1);
    const active = jobs.find((j) => j.status === "running" || j.status === "queued");
    const cap2 = jobs.filter((j) => j.kind === "caption" || j.kind === "pipeline").at(-1);
    const win = cut?.result;
    const mode = (cut?.params?.mode as string) || (jobs.find((j) => j.kind === "moments")?.result.mode as string) || "";
    const status = render ? "ready" : active ? "rendering" : "queued";
    out.push({
      id: cid,
      title: clipTitle(jobs, cid),
      src: cut?.source_id || jobs[0]?.source_id || "",
      dur: win?.start != null && win?.end != null ? win.end - win.start : 0,
      aspect: (render?.result.aspect as string) || (cut?.params?.aspect as string) || "9:16",
      style: (cap2?.result.style as string) || (cap2?.params?.style as string) || "opus",
      platform: PLAT_OF[(render?.result.preset as string) || ""] || "tiktok",
      status, prog: active?.progress_pct ?? 0, renderId: render?.result.render_id,
      tags: mode ? [cap(mode)] : [],
      start: win?.start, end: win?.end,
    });
  }
  return out.reverse();
}

function mapJobs(snap: EventsSnapshot | null): SpoolJob[] {
  if (!snap) return [];
  const jobs: SpoolJob[] = [];
  for (const j of snap.jobs) {
    if (j.status === "done")
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: "done", prog: 100, stage: "complete", eta: "—", elapsed: j.human?.elapsed || "—" });
    else if (j.status === "error" || j.status === "cancelled")
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: "failed", prog: j.progress_pct, stage: j.error_message || "error", eta: "—", elapsed: j.human?.elapsed || "—", err: true });
    else if (j.status === "paused")
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: "paused", prog: j.progress_pct, stage: "paused", eta: "—", elapsed: j.human?.elapsed || "—" });
    else
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: j.status === "downloading" ? "running" : "queued", prog: j.progress_pct, stage: j.human?.summary || "downloading", eta: j.human?.eta || "—", elapsed: j.human?.elapsed || "—" });
  }
  for (const t of snap.transcripts)
    if (t.status === "running" || t.status === "queued")
      jobs.push({ id: t.id, type: "transcribe", domain: "transcribe", label: t.human?.summary || "transcribe", src: t.parent_job_id, status: t.status === "running" ? "running" : "queued", prog: t.progress_pct, stage: "whisper · on-device", eta: "—", elapsed: t.human?.elapsed || "—" });
  for (const c of snap.clips) {
    if (c.status === "done" && c.kind === "moments") continue;
    const type = c.kind === "moments" ? "transcribe" : "render";
    const st = c.status === "running" ? "running" : c.status === "queued" ? "queued" : c.status === "done" ? "done" : c.status === "error" ? "failed" : c.status;
    if (st === "done" || st === "failed" || st === "running" || st === "queued")
      jobs.push({ id: c.id, type, domain: "clip", label: `${cap(c.kind)} · ${(c.clip_id || c.source_id || "").slice(0, 8)}`, src: c.source_id || "", status: st, prog: c.progress_pct, stage: c.stage || c.error_message || c.kind, eta: "—", elapsed: c.human?.elapsed || "—", err: c.status === "error" });
  }
  return jobs;
}

function mapDownloads(snap: EventsSnapshot | null): SpoolDownload[] {
  if (!snap) return [];
  return snap.jobs.map((j) => ({
    id: j.id, title: j.title || j.url, src: originOf(j.url), prog: j.progress_pct,
    status: j.status === "done" ? "done" : j.status === "error" ? "error" : j.status === "cancelled" ? "error" : "downloading",
    size: j.human?.size || "", speed: j.human?.speed || "", eta: j.human?.eta || "—", err: j.error_message,
  })).reverse();
}

/** Build the demo's Candidate shape from a source's `find_moments` results.
 *  `signals` are the real glass-box reasons; `excerpt` is the real transcript text in range.
 *
 *  Accumulates across EVERY completed scan for the source (oldest→newest), not just the latest —
 *  so scanning another mode ADDS to the pool instead of wiping it, and the mode tabs filter a
 *  growing set. Deduped by mode+range so re-running a mode doesn't double up. */
export function mapCandidates(snap: EventsSnapshot | null, sourceId: string | undefined, words?: TranscriptWord[]): Candidate[] {
  if (!snap || !sourceId) return [];
  const jobs = snap.clips.filter(
    (c) => c.kind === "moments" && c.source_id === sourceId && c.status === "done" && c.result.candidates?.length,
  );
  const out: Candidate[] = [];
  const seen = new Set<string>();
  for (const job of jobs) {
    (job.result.candidates ?? []).forEach((m, i) => {
      const key = `${(m.mode || "").toLowerCase()}|${Math.round(m.start)}|${Math.round(m.end)}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({
        id: `${job.id}-${i}`, title: m.title, start: m.start, end: m.end, mode: cap(m.mode),
        why: m.rationale, excerpt: words ? excerptFor(words, m.start, m.end) : "", signals: m.signals ?? [],
        sel: out.length < 3, source_id: sourceId, // the first few stay pre-selected for a quick "Make clips"
        score: m.score, factors: m.factors, weights: m.weights, // the real glass-box rank (engine moments.rank)
      });
    });
  }
  return out;
}

function excerptFor(words: TranscriptWord[], start: number, end: number): string {
  const span = words.filter((w) => w.start != null && w.start >= start && w.start <= end && !w.deleted).map((w) => w.w);
  let text = span.join(" ").replace(/\s+([.,!?])/g, "$1").trim();
  if (text.length > 180) text = text.slice(0, 177).trimEnd() + "…";
  return text;
}

export interface TranscriptLine { id: number; sp: string; t: number; words: string; tokens: { w: string; ti: number; te: number; idx: number }[] }
export interface SpeakerInfo { name: string; color: string }
const ROI_COLORS = ["var(--roi-l)", "var(--roi-r)", "var(--accent)", "var(--warn)", "var(--ok)"];

/** Group a transcript's words.json into speaker-attributed lines for the TranscriptView. */
export function buildTranscript(words: TranscriptWord[] | undefined): { lines: TranscriptLine[]; speakers: Record<string, SpeakerInfo> } {
  if (!words?.length) return { lines: [], speakers: {} };
  const live = words.filter((w) => !w.deleted && w.w.trim());
  const speakerKeys = [...new Set(live.map((w) => w.speaker || "A"))];
  const speakers: Record<string, SpeakerInfo> = {};
  speakerKeys.forEach((k, i) => (speakers[k] = { name: k.length <= 2 ? `Speaker ${k}` : k, color: ROI_COLORS[i % ROI_COLORS.length] }));
  const lines: TranscriptLine[] = [];
  let cur: TranscriptLine | null = null;
  let id = 0;
  for (const w of live) {
    const sp = w.speaker || "A";
    const ti: number = w.start ?? cur?.t ?? 0;
    if (!cur || cur.sp !== sp || ti - (cur.tokens.at(-1)?.ti ?? ti) > 2.5) {
      cur = { id: id++, sp, t: ti, words: "", tokens: [] };
      lines.push(cur);
    }
    cur.tokens.push({ w: w.w, ti, te: w.end ?? ti, idx: w.idx });
    cur.words += (cur.words ? " " : "") + w.w;
  }
  return { lines, speakers };
}

interface SpoolCtx {
  client: SpoolApiClient;
  sources: SpoolSource[];
  clips: SpoolClip[];
  jobs: SpoolJob[];
  downloads: SpoolDownload[];
  recipes: string[];
  deps: SpoolDep[];
  snapshot: EventsSnapshot | null;
  nav: (screen: string, params?: { id?: string; tab?: string }) => void;
  agentOpen: boolean; openAgent: () => void; toggleAgent: () => void; closeAgent: () => void;
  paletteOpen: boolean; openPalette: () => void; closePalette: () => void;
  shortcutsOpen: boolean; openShortcuts: () => void; closeShortcuts: () => void;
  agentMessages: AgentMessage[]; working: boolean;
  askAgent: (text: string, sourceId?: string) => void;
  answerElicit: (msg: AgentMessage, answer: unknown) => void;
  makeClipsFrom: (sel: { source_id?: string; start?: number; end?: number; id?: string; title?: string }[], opts?: { aspect?: string; mode?: string; style?: string; preset?: string }) => void;
  toasts: Toast[]; pushToast: (t: Omit<Toast, "id">) => void;
  offline: boolean; toggleOffline: () => void;
}

const Ctx = createContext<SpoolCtx | null>(null);

const ROUTE: Record<string, string> = {
  home: "/", import: "/import", library: "/library", clips: "/clips", queue: "/queue",
  settings: "/settings", publish: "/publish", analytics: "/analytics", brand: "/brand", onboarding: "/onboarding",
};

export function SpoolProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const client = useEngine();
  const { snapshot } = useLive();
  const doctor = useEngineQuery((c) => c.doctor());

  const [agentOpen, setAgentOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [agentMessages, setAgentMessages] = useState<AgentMessage[]>(INITIAL_AGENT);
  const [working, setWorking] = useState(false);
  const [offline, setOffline] = useState(true);
  const elicitSeq = useRef(0); // monotonic, collision-free ids for elicitation cards

  const pushToast = (t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { ...t, id }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), 4200);
  };

  const nav = (screen: string, params: { id?: string; tab?: string } = {}) => {
    const id = params.id;
    const q = params.tab ? `?tab=${encodeURIComponent(params.tab)}` : "";
    switch (screen) {
      case "project": return router.push(id ? `/sources/${id}${q}` : "/library");
      case "discovery": return router.push(id ? `/sources/${id}/discovery` : "/library");
      case "editor": return router.push(id ? `/clips/${id}` : "/clips");
      case "reframe": return router.push(id ? `/clips/${id}/reframe` : "/clips");
      case "caption": return router.push(id ? `/clips/${id}/caption` : "/clips");
      default: return router.push(ROUTE[screen] ?? "/");
    }
  };

  const push = (m: AgentMessage) => setAgentMessages((a) => [...a, m]);

  const askAgent = (text: string, sourceId?: string) => {
    if (!text.trim()) return;
    setAgentOpen(true);
    push({ role: "user", text });
    setWorking(true);
    client
      .agent(text, sourceId ? { sourceId } : {})
      .then((r) => {
        setWorking(false);
        push({ role: "agent", text: r.reply });
        if (r.jobs?.length) {
          push({ role: "trace", tools: r.jobs.map((j) => ({ name: j.kind, arg: j.clip_id ? "· " + j.clip_id.slice(0, 6) : "", ms: Math.round(j.elapsed_seconds * 1000) })) });
          pushToast({ icon: "sparkles", tone: "info", title: `Agent started ${r.jobs.length} job${r.jobs.length > 1 ? "s" : ""}`, body: "Track them in the Render Queue" });
        }
        if (r.action === "clarify" && r.question)
          push({ role: "elicit", id: "e" + ++elicitSeq.current, kind: "enum", tag: "agent needs you", q: r.question, options: r.options ?? [], sourceId });
      })
      .catch(() => {
        setWorking(false);
        push({ role: "agent", text: "I couldn't reach the engine just now. Make sure it's running, then try again." });
      });
  };

  const answerElicit = (msg: AgentMessage, answer: unknown) => {
    setAgentMessages((a) => a.map((m) => (m === msg || (msg.id && m.id === msg.id) ? { ...m, answered: true, answer } : m)));
    const text = Array.isArray(answer) ? answer.join(", ") : String(answer ?? "");
    if (text) askAgent(text, msg.sourceId); // re-ask with the source context the clarify was raised in
  };

  /** Poll a clip job until it leaves the running/queued state, so a dependent next step (caption
   *  after reframe, export after caption) starts only once its input file is fully written. */
  const awaitClipJob = async (id?: string): Promise<void> => {
    if (!id) return;
    for (let i = 0; i < 600; i++) {
      const j = await client.getClipJob(id).catch(() => null);
      if (!j || j.status === "done" || j.status === "error" || j.status === "cancelled") return;
      await new Promise((r) => setTimeout(r, 1000));
    }
  };

  /** Two paths, by what's passed:
   *  - Discovery candidates (have source_id + start/end) → CUT + auto-reframe to 9:16 and STOP
   *    (no burn), then land in the source's Clips tab to review. Nothing renders yet — you check
   *    each clip first (the user-requested flow).
   *  - An existing clip (the Editor's Render, id only) → caption (chosen style) + export, and go
   *    to the Render Queue to watch it. Reframe first if the Editor changed the format. */
  const makeClipsFrom = (
    sel: { source_id?: string; start?: number; end?: number; id?: string; title?: string }[],
    opts: { aspect?: string; mode?: string; style?: string; preset?: string } = {},
  ) => {
    const aspect = opts.aspect ?? "9:16", mode = opts.mode ?? "pan", style = opts.style ?? "opus", preset = opts.preset ?? "tiktok";
    const fresh = sel.filter((c) => c.source_id && c.start != null && c.end != null);
    const existing = sel.filter((c) => c.id && !(c.source_id && c.start != null && c.end != null));

    if (fresh.length) {
      for (const c of fresh)
        client.renderPipeline(c.source_id!, { start: c.start!, end: c.end!, aspect, mode, stop_after: "reframe" }).catch(() => {});
      pushToast({ icon: "scissors", tone: "info", title: `Cutting ${fresh.length} clip${fresh.length > 1 ? "s" : ""}`,
        body: "Auto-reframing to 9:16 — review each in the Clips tab, then render when you're happy." });
      nav("project", { id: fresh[0].source_id!, tab: "Clips" });
      return;
    }
    if (!existing.length) { pushToast({ icon: "alert", tone: "warn", title: "Nothing to render", body: "No clip or moment range to act on." }); return; }
    for (const c of existing) {
      // Burn the chosen caption style, then export — reframe first if the Editor changed the
      // format. These are separate engine jobs that mutate the same files in sequence, so each
      // MUST finish before the next starts (otherwise caption reads a half-written reframe →
      // "moov atom not found"). Await each job's completion; the user watches it in the Queue.
      void (async () => {
        try {
          if (opts.aspect || opts.mode) await awaitClipJob((await client.reframe(c.id!, { aspect, mode }))?.id);
          await awaitClipJob((await client.caption(c.id!, { style }))?.id);
          await client.render(c.id!, { preset });
        } catch { /* surfaced as an errored job in the queue */ }
      })();
    }
    pushToast({ icon: "film", tone: "info", title: `Rendering ${existing.length} clip${existing.length > 1 ? "s" : ""}`, body: "Burning captions + exporting — track it in the Render Queue" });
    setAgentOpen(true);
    nav("queue");
  };

  const deps: SpoolDep[] = doctor.data
    ? Object.entries(doctor.data.tools).map(([id, t]) => ({
        id, name: id, note: "", status: t.present ? "ok" : "missing", ver: t.version || "—",
      }))
    : [];

  const value: SpoolCtx = {
    client,
    sources: mapSources(snapshot), clips: mapClips(snapshot), jobs: mapJobs(snapshot),
    downloads: mapDownloads(snapshot), recipes: RECIPES, deps, snapshot,
    nav, agentOpen, openAgent: () => setAgentOpen(true), toggleAgent: () => setAgentOpen((o) => !o), closeAgent: () => setAgentOpen(false),
    paletteOpen, openPalette: () => setPaletteOpen(true), closePalette: () => setPaletteOpen(false),
    shortcutsOpen, openShortcuts: () => setShortcutsOpen(true), closeShortcuts: () => setShortcutsOpen(false),
    agentMessages, working, askAgent, answerElicit, makeClipsFrom,
    toasts, pushToast, offline, toggleOffline: () => setOffline((o) => !o),
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSpool(): SpoolCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useSpool must be used within <SpoolProvider>");
  return c;
}
