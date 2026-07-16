"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { SpoolApiError, type SpoolApiClient } from "@spool/api-client";
import type { ClipJobView, EventsSnapshot, RankFactors, TranscriptWord } from "@spool/types";
import { useEngine, useEngineQuery, useLive } from "@/lib/engine-context";
import { formatActionError } from "@/lib/action-error";

/* The demo's `useSpool()` context, backed by the LIVE engine instead of mock data.
 * Maps the SSE snapshot into the demo's source/clip/job shapes so the ported demo
 * components render unchanged. `nav` drives Next routes; the agent loop calls the real
 * `/agent` endpoint and elicitation = the agent's `clarify` turn; "make clips" runs real
 * render pipelines. Per-source data (candidates, transcript) is mapped on demand by the
 * detail pages. Zero mock data (spec §6.2). */

export interface SpoolSource {
  id: string; title: string; src: string; dur: number; status: string;
  prog?: number; clips: number; kind: string; channel: string; res: string; fps?: number;
  size: string; lang: string; added: string; scenes?: number; transcriptId?: string; speakerCount?: number;
}
export interface SpoolClip {
  id: string; title: string; src: string; dur: number; aspect?: string; style?: string;
  platform?: string; status: string; prog?: number; tags?: string[]; renderId?: string; score?: number;
  start?: number; end?: number; // the cut window in source time, for slicing the transcript
}
export interface SpoolJob {
  id: string; type: string; label: string; src: string; status: string; prog: number; stage: string; eta: string; elapsed: string; err?: boolean;
  errorCode?: string | null; errorMessage?: string | null;
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

/** The six glass-box ranking factors (engine snake_case keys), in display order. */
export const RANK_FACTORS = ["hook", "self_contained", "arc", "energy", "length_fit", "boundary_quality"] as const;

/** The engine's DEFAULT_WEIGHTS (clip.moments.DEFAULT_WEIGHTS: hook .30 / self_contained .25 /
 *  energy .20 / arc .15 / length_fit .05 / boundary_quality .05), expressed as INTEGER ratios that
 *  fit the sliders and normalize to the same proportions (6:5:4:3:1:1 → the engine's weights). The
 *  single source of truth for both reweight panels (Discovery + Recipes) and the all-zero fallback
 *  below, so the studio's initial ranking matches the engine. */
export const ENGINE_DEFAULT_WEIGHTS: Record<string, number> = {
  hook: 6, self_contained: 5, energy: 4, arc: 3, length_fit: 1, boundary_quality: 1,
};

/** Client mirror of the engine's transparent score: round(100 · Σ(factorₖ·weightₖ) / Σweightₖ)
 *  over all six RANK_FACTORS (engine-consistent normalization), factors in [0,1]. Same math as
 *  `clip.moments.rank`, so the Discovery reweight slider stays instant (no server round-trip per
 *  tick, spec §6.4); this mirrors the engine's ordering, integer-rounded for display (the engine
 *  rounds to 1 decimal). An all-zero weight vector falls back to ENGINE_DEFAULT_WEIGHTS, exactly as
 *  the engine's `_normalized_weights` does — so it scores like the default, never 0. */
export function scoreFromFactors(factors: RankFactors = {}, weights: RankFactors = {}): number {
  const f = factors as Record<string, number | undefined>;
  let w = weights as Record<string, number | undefined>;
  let tw = RANK_FACTORS.reduce((a, k) => a + (w[k] ?? 0), 0);
  if (tw <= 0) { w = ENGINE_DEFAULT_WEIGHTS; tw = RANK_FACTORS.reduce((a, k) => a + (w[k] ?? 0), 0); }
  return Math.round((100 * RANK_FACTORS.reduce((a, k) => a + (f[k] ?? 0) * (w[k] ?? 0), 0)) / tw);
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
  /** set on a confirm elicit: re-ask payload for the approved turn */
  confirmFor?: { text: string; tool: string };
}

const INITIAL_AGENT: AgentMessage[] = [
  { role: "agent", text: "Hi — I'm your read-only clip assistant. I can inspect your sources, transcripts, clips, and render queue, then explain what is happening. Changes stay in your hands." },
];

function originOf(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "file:") return "file";
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return undefined;
    const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    const isHost = (domain: string) => host === domain || host.endsWith(`.${domain}`);
    if (isHost("youtube.com") || isHost("youtube-nocookie.com") || isHost("youtu.be")) return "youtube";
    if (isHost("instagram.com")) return "instagram";
    if (isHost("tiktok.com")) return "tiktok";
    if (isHost("x.com") || isHost("twitter.com")) return "x";
  } catch {
    // An unparseable or non-URL source is unknown, not proof that it is a local file.
  }
  return undefined;
}
const cap = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const human = (bytes: number) => {
  if (!bytes) return "—";
  const u = ["B", "KB", "MB", "GB"]; let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};

export function mapSources(snap: EventsSnapshot | null): SpoolSource[] {
  if (!snap) return [];
  const clipCount = (sid: string) =>
    new Set(snap.clips.filter((c) => c.source_id === sid && c.clip_id).map((c) => c.clip_id)).size;
  return snap.jobs
    .filter((j) => j.status === "done" && j.filename)
    .map((j) => {
      // Snapshot order is manager insertion order; a re-transcribe appends a new attempt.
      // elapsed_seconds is run duration, not recency, so sorting by it can resurrect an old try.
      const attempts = snap.transcripts.filter((t) => t.parent_job_id === j.id);
      const currentAttempt = attempts.at(-1);
      const latestSuccessful = attempts.filter((attempt) => attempt.status === "done").at(-1);
      const activeAttempt = currentAttempt && (currentAttempt.status === "running" || currentAttempt.status === "queued");
      const status = activeAttempt ? "transcribing" : latestSuccessful ? "ready" : "downloaded";
      const speakers = latestSuccessful?.speaker_count && latestSuccessful.speaker_count > 0 ? latestSuccessful.speaker_count : undefined;
      const origin = originOf(j.url);
      return {
        id: j.id, title: j.title || j.url, src: origin ?? "—",
        dur: latestSuccessful?.duration_seconds || 0, status, prog: activeAttempt ? currentAttempt.progress_pct : latestSuccessful?.progress_pct ?? currentAttempt?.progress_pct ?? 0,
        clips: clipCount(j.id),
        kind: speakers ? `${speakers} speaker${speakers === 1 ? "" : "s"}` : "—",
        channel: origin === "file" ? "local file" : origin ?? "—",
        res: "—", size: human(j.total_bytes || j.downloaded_bytes),
        lang: latestSuccessful?.language_detected || "—", added: "—",
        transcriptId: latestSuccessful?.id, speakerCount: speakers,
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

export function mapClips(snap: EventsSnapshot | null): SpoolClip[] {
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
    const active = jobs.filter((j) => j.status === "running" || j.status === "queued").at(-1);
    const reframe = jobs.filter((j) => (j.kind === "reframe" || j.kind === "pipeline") && j.status === "done").at(-1);
    const cap2 = jobs.filter((j) => (j.kind === "caption" || j.kind === "pipeline") && j.status === "done").at(-1);
    const latest = jobs.at(-1);
    const win = cut?.result;
    const mode = (cut?.params?.mode as string) || (jobs.find((j) => j.kind === "moments")?.result.mode as string) || "";
    const status = render
      ? "ready"
      : active
        ? active.status === "queued" ? "queued" : "rendering"
        : latest?.status === "error" || latest?.status === "cancelled"
          ? latest.status
          : cut?.status === "done" ? "ready" : "—";
    const aspect = (render?.result.aspect as string | undefined)
      ?? (reframe?.result.aspect as string | undefined)
      ?? (reframe?.params?.aspect as string | undefined);
    const style = (cap2?.result.style as string | undefined)
      ?? (cap2?.params?.style as string | undefined);
    const renderPreset = (render?.result.preset as string | undefined)
      ?? (render?.params?.preset as string | undefined);
    out.push({
      id: cid,
      title: clipTitle(jobs, cid),
      src: cut?.source_id || jobs[0]?.source_id || "",
      dur: win?.start != null && win?.end != null ? win.end - win.start : 0,
      aspect,
      style,
      platform: renderPreset ? PLAT_OF[renderPreset] : undefined,
      status, prog: active?.progress_pct ?? 0, renderId: render?.result.render_id,
      tags: mode ? [cap(mode)] : [],
      start: win?.start, end: win?.end,
    });
  }
  return out.reverse();
}

export function mapJobs(snap: EventsSnapshot | null): SpoolJob[] {
  if (!snap) return [];
  const jobs: SpoolJob[] = [];
  for (const j of snap.jobs) {
    if (j.dismissed) continue;
    if (j.status === "done")
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: "done", prog: 100, stage: "complete", eta: "—", elapsed: j.human?.elapsed || "—" });
    else if (j.status === "error")
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: "failed", prog: j.progress_pct, stage: j.error_message || "error", eta: "—", elapsed: j.human?.elapsed || "—", err: true, errorCode: j.error_category, errorMessage: j.error_message });
    else if (j.status === "cancelled")
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: "cancelled", prog: j.progress_pct, stage: j.error_message || "cancelled", eta: "—", elapsed: j.human?.elapsed || "—", errorCode: j.error_category, errorMessage: j.error_message });
    else if (j.status === "paused")
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: "paused", prog: j.progress_pct, stage: "paused", eta: "—", elapsed: j.human?.elapsed || "—" });
    else
      jobs.push({ id: j.id, type: "download", domain: "download", label: j.title || j.url, src: j.id, status: j.status === "downloading" ? "running" : "queued", prog: j.progress_pct, stage: j.human?.summary || "downloading", eta: j.human?.eta || "—", elapsed: j.human?.elapsed || "—" });
  }
  for (const t of snap.transcripts) {
    if (t.dismissed) continue;
    if (t.status === "running" || t.status === "queued")
      jobs.push({ id: t.id, type: "transcribe", domain: "transcribe", label: t.human?.summary || "transcribe", src: t.parent_job_id, status: t.status === "running" ? "running" : "queued", prog: t.progress_pct, stage: "whisper · on-device", eta: "—", elapsed: t.human?.elapsed || "—" });
    else if (t.status === "error" || t.status === "cancelled")
      jobs.push({ id: t.id, type: "transcribe", domain: "transcribe", label: t.human?.summary || "transcribe", src: t.parent_job_id, status: t.status === "error" ? "failed" : "cancelled", prog: t.progress_pct, stage: t.error_message || (t.status === "error" ? "transcription failed" : "cancelled"), eta: "—", elapsed: t.human?.elapsed || "—", err: t.status === "error", errorCode: t.error_category, errorMessage: t.error_message });
  }
  for (const c of snap.clips) {
    if (c.dismissed) continue;
    if (c.status === "done" && c.kind === "moments") continue;
    const type = c.kind === "moments" ? "analysis" : "render";
    const st = c.status === "running" ? "running" : c.status === "queued" ? "queued" : c.status === "done" ? "done" : c.status === "error" ? "failed" : c.status;
    if (st === "done" || st === "failed" || st === "cancelled" || st === "running" || st === "queued")
      jobs.push({ id: c.id, type, domain: "clip", label: `${cap(c.kind)} · ${(c.clip_id || c.source_id || "").slice(0, 8)}`, src: c.source_id || "", status: st, prog: c.progress_pct, stage: (c.status === "error" || c.status === "cancelled" ? c.error_message : null) || c.stage || c.kind, eta: "—", elapsed: c.human?.elapsed || "—", err: c.status === "error", errorCode: c.error_category, errorMessage: c.error_message });
  }
  return jobs;
}

export function mapDownloads(snap: EventsSnapshot | null): SpoolDownload[] {
  if (!snap) return [];
  return snap.jobs.filter((j) => !j.dismissed).map((j) => ({
    id: j.id, title: j.title || j.url, src: originOf(j.url) ?? "—", prog: j.progress_pct,
    status: j.status,
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
  const speakerKeys = [...new Set(live.map((w) => w.speaker || "unknown"))];
  const speakers: Record<string, SpeakerInfo> = {};
  speakerKeys.forEach((k, i) => (speakers[k] = {
    name: k === "unknown" ? "Unknown speaker" : k.length <= 2 ? `Speaker ${k}` : k,
    color: ROI_COLORS[i % ROI_COLORS.length]!,
  }));
  const lines: TranscriptLine[] = [];
  let cur: TranscriptLine | null = null;
  let id = 0;
  for (const w of live) {
    const sp = w.speaker || "unknown";
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
  deps: SpoolDep[];
  snapshot: EventsSnapshot | null;
  nav: (screen: string, params?: { id?: string; tab?: string }) => void;
  agentOpen: boolean; openAgent: () => void; toggleAgent: () => void; closeAgent: () => void;
  paletteOpen: boolean; openPalette: () => void; closePalette: () => void;
  shortcutsOpen: boolean; openShortcuts: () => void; closeShortcuts: () => void;
  agentMessages: AgentMessage[]; working: boolean;
  askAgent: (text: string, sourceId?: string, confirmTool?: string) => void;
  answerElicit: (msg: AgentMessage, answer: unknown) => void;
  makeClipsFrom: (sel: { source_id?: string; start?: number; end?: number; id?: string; title?: string }[], opts?: { aspect?: string; mode?: string; style?: string; preset?: string }) => Promise<void>;
  /** Poll a clip job to a terminal state — pages sequence dependent jobs with it. */
  awaitClipJob: (id?: string) => Promise<void>;
  toasts: Toast[]; pushToast: (t: Omit<Toast, "id">) => void;
  offline: boolean; offlinePending: boolean; toggleOffline: () => void;
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
  // Offline mode is the persisted engine setting (drives SPOOL_OFFLINE), NOT local state —
  // the toggle has to actually gate egress, and the badge must show engine-truth. Honest
  // default is false (the Codex bridge IS egress) until the user opts in.
  const settingsQ = useEngineQuery((c) => c.getSettings());
  const offline = settingsQ.data?.offline ?? false;
  const [offlinePending, setOfflinePending] = useState(false);
  const offlineInFlight = useRef(false);
  const providerMounted = useRef(false);
  useEffect(() => {
    providerMounted.current = true;
    return () => { providerMounted.current = false; };
  }, []);
  // useEngineQuery returns a fresh `reload` each render; hold the latest in a ref (written in
  // an effect, not in render) so toggleOffline stays referentially stable — only `offline`
  // should churn the context value, not reload's identity.
  const reloadSettingsRef = useRef(settingsQ.reload);
  useEffect(() => { reloadSettingsRef.current = settingsQ.reload; });
  const elicitSeq = useRef(0); // monotonic, collision-free ids for elicitation cards
  const agentInFlight = useRef(false);

  const pushToast = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { ...t, id }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), 4200);
  }, []);

  const toggleOffline = useCallback(() => {
    if (offlineInFlight.current) return;
    offlineInFlight.current = true;
    setOfflinePending(true);
    void client
      .updateSettings({ offline: !offline })
      .then(() => reloadSettingsRef.current())
      .catch((error: unknown) => pushToast({
        icon: "alert",
        tone: "warn",
        title: "Couldn't update offline mode",
        body: formatActionError(error),
      }))
      .finally(() => {
        offlineInFlight.current = false;
        if (providerMounted.current) setOfflinePending(false);
      });
  }, [client, offline, pushToast]);

  const nav = useCallback((screen: string, params: { id?: string; tab?: string } = {}) => {
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
  }, [router]);

  const push = useCallback((m: AgentMessage) => setAgentMessages((a) => [...a, m]), []);

  const askAgent = useCallback((text: string, sourceId?: string, confirmTool?: string) => {
    if (!text.trim() || agentInFlight.current) return;
    agentInFlight.current = true;
    setAgentOpen(true);
    push({ role: "user", text });
    setWorking(true);
    client
      .agent(text, { sourceId, confirmTool })
      .then((r) => {
        // Real per-step tool trace from the ReAct loop (read tools that start no job are visible too).
        if (r.tools?.length)
          push({ role: "trace", tools: r.tools.map((t) => ({ name: t.name, arg: t.arg ?? "", ms: t.ms ?? 0 })) });
        push({ role: "agent", text: r.reply });
        if (r.action === "clarify" && r.question)
          push({ role: "elicit", id: "e" + ++elicitSeq.current, kind: (r.kind as AgentMessage["kind"]) ?? "enum", tag: "agent needs you", q: r.question, options: r.options ?? [], sourceId });
        if (r.action === "confirm" && r.pending)
          push({
            role: "elicit", id: "e" + ++elicitSeq.current, kind: "confirm",
            tag: "agent needs approval",
            q: r.question || `Allow ${r.pending.tool}?`,
            options: r.options ?? ["Confirm", "Cancel"], yes: "Confirm",
            sourceId, confirmFor: { text, tool: r.pending.tool },
          });
      })
      .catch((error: unknown) => {
        push({ role: "agent", text: formatActionError(error) });
      })
      .finally(() => {
        agentInFlight.current = false;
        setWorking(false);
      });
  }, [client, push]);

  const answerElicit = useCallback((msg: AgentMessage, answer: unknown) => {
    setAgentMessages((a) => a.map((m) => (m === msg || (msg.id && m.id === msg.id) ? { ...m, answered: true, answer } : m)));
    if (msg.confirmFor) {
      // Approval re-runs the SAME message with the tool pre-approved for one call — the
      // loop re-plans, so args may differ from the pending preview; the gate's contract
      // is "no gated tool without a human click", not arg-exact replay.
      const approved = String(answer).toLowerCase() === "yes";
      if (approved) askAgent(msg.confirmFor.text, msg.sourceId, msg.confirmFor.tool);
      else push({ role: "agent", text: "Cancelled — nothing was run." });
      return;
    }
    const text = Array.isArray(answer) ? answer.join(", ") : String(answer ?? "");
    if (text) askAgent(text, msg.sourceId); // re-ask with the source context the clarify was raised in
  }, [askAgent, push]);

  /** Poll a clip job until it leaves the running/queued state, so a dependent next step (caption
   *  after reframe, export after caption) starts only once its input file is fully written. */
  const awaitClipJob = useCallback(async (id?: string): Promise<void> => {
    if (!id) return;
    for (let i = 0; i < 600; i++) {
      const j = await client.getClipJob(id);
      if (j.status === "done") return;
      if (j.status === "error") throw new SpoolApiError(409, j.error_category || "clip_job_error", j.error_message || `Clip job ${id} failed.`);
      if (j.status === "cancelled") throw new SpoolApiError(409, "cancelled", `Clip job ${id} was cancelled.`);
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new SpoolApiError(0, "timeout", `Timed out waiting for clip job ${id}.`);
  }, [client]);

  /** Two paths, by what's passed:
   *  - Discovery candidates (have source_id + start/end) → CUT + auto-reframe to 9:16 and STOP
   *    (no burn), then land in the source's Clips tab to review. Nothing renders yet — you check
   *    each clip first (the user-requested flow).
   *  - An existing clip (the Editor's Render, id only) → caption (chosen style) + export, and go
   *    to the Render Queue to watch it. Reframe first if the Editor changed the format. */
  const makeClipsFrom = useCallback(async (
    sel: { source_id?: string; start?: number; end?: number; id?: string; title?: string }[],
    opts: { aspect?: string; mode?: string; style?: string; preset?: string } = {},
  ): Promise<void> => {
    const startedAtLocation = window.location.href;
    const aspect = opts.aspect ?? "9:16", mode = opts.mode ?? "pan", style = opts.style ?? "opus", preset = opts.preset ?? "tiktok";
    const fresh = sel.filter((c) => c.source_id && c.start != null && c.end != null);
    const existing = sel.filter((c) => c.id && !(c.source_id && c.start != null && c.end != null));

    if (fresh.length) {
      const results = await Promise.allSettled(
        fresh.map((c) =>
          client.renderPipeline(c.source_id!, { start: c.start!, end: c.end!, aspect, mode, stop_after: "reframe" }),
        ),
      );
      const failed = results.filter((result) => result.status === "rejected");
      const succeeded = results.length - failed.length;
      pushToast({
        icon: failed.length ? "alert" : "scissors",
        tone: failed.length ? "warn" : "ok",
        title: `${succeeded} clip${succeeded === 1 ? "" : "s"} started · ${failed.length} failed`,
        body: failed[0]
          ? formatActionError(failed[0].reason, "A clip could not be started.")
          : "Every clip was accepted by the render queue.",
      });
      if (succeeded > 0 && window.location.href === startedAtLocation)
        nav("project", { id: fresh[0]!.source_id!, tab: "Clips" });
      return;
    }
    if (!existing.length) { pushToast({ icon: "alert", tone: "warn", title: "Nothing to render", body: "No clip or moment range to act on." }); return; }
    const results = await Promise.allSettled(existing.map(async (c) => {
      if (opts.aspect || opts.mode) await awaitClipJob((await client.reframe(c.id!, { aspect, mode })).id);
      await awaitClipJob((await client.caption(c.id!, { style })).id);
      await client.render(c.id!, { preset });
    }));
    const failed = results.filter((result) => result.status === "rejected");
    const succeeded = results.length - failed.length;
    pushToast({
      icon: failed.length ? "alert" : "film",
      tone: failed.length ? "warn" : "ok",
      title: `${succeeded} render${succeeded === 1 ? "" : "s"} started · ${failed.length} failed`,
      body: failed[0]
        ? formatActionError(failed[0].reason, "A render chain could not finish.")
        : "Every render was accepted by the queue.",
    });
    if (succeeded > 0 && window.location.href === startedAtLocation) nav("queue");
  }, [client, nav, pushToast, awaitClipJob]);

  // Mappers walk the full snapshot; unmemoized they re-ran 4x on EVERY provider render —
  // including renders caused by toasts/panel state — and the ~1Hz SSE delta churned every
  // useSpool() consumer. Keyed on the snapshot: identity is stable between frames.
  const sources = useMemo(() => mapSources(snapshot), [snapshot]);
  const clips = useMemo(() => mapClips(snapshot), [snapshot]);
  const jobs = useMemo(() => mapJobs(snapshot), [snapshot]);
  const downloads = useMemo(() => mapDownloads(snapshot), [snapshot]);
  const deps = useMemo<SpoolDep[]>(
    () =>
      doctor.data
        ? Object.entries(doctor.data.tools).map(([id, t]) => ({
            id, name: id, note: "", status: t.present ? "ok" : "missing", ver: t.version || "—",
          }))
        : [],
    [doctor.data],
  );

  const value = useMemo<SpoolCtx>(
    () => ({
      client,
      sources, clips, jobs, downloads, deps, snapshot,
      nav, agentOpen, openAgent: () => setAgentOpen(true), toggleAgent: () => setAgentOpen((o) => !o), closeAgent: () => setAgentOpen(false),
      paletteOpen, openPalette: () => setPaletteOpen(true), closePalette: () => setPaletteOpen(false),
      shortcutsOpen, openShortcuts: () => setShortcutsOpen(true), closeShortcuts: () => setShortcutsOpen(false),
      agentMessages, working, askAgent, answerElicit, makeClipsFrom, awaitClipJob,
      toasts, pushToast, offline, offlinePending, toggleOffline,
    }),
    [
      client, sources, clips, jobs, downloads, deps, snapshot, nav, agentOpen, paletteOpen,
      shortcutsOpen, agentMessages, working, askAgent, answerElicit, makeClipsFrom, awaitClipJob,
      toasts, pushToast, offline, offlinePending, toggleOffline,
    ],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSpool(): SpoolCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useSpool must be used within <SpoolProvider>");
  return c;
}
