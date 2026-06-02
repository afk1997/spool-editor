"use client";

import { createContext, useContext, useState } from "react";
import { useRouter } from "next/navigation";
import type { ClipJobView, EventsSnapshot } from "@spool/types";
import { useEngine, useEngineQuery, useLive } from "@/lib/engine-context";

/* The demo's `useSpool()` context, backed by the LIVE engine instead of mock data.
 * Maps the SSE snapshot into the demo's source/clip/job/dep shapes so the ported demo
 * components render unchanged. `nav` drives Next routes; agent/palette/toast state is local. */

export interface SpoolSource {
  id: string; title: string; src: string; dur: number; status: string;
  prog?: number; clips: number; kind: string;
}
export interface SpoolClip {
  id: string; title: string; src: string; dur: number; aspect: string; style: string;
  platform: string; status: string; prog?: number; tags?: string[]; renderId?: string;
}
export interface SpoolJob {
  id: string; type: string; label: string; src: string; status: string; prog: number; stage: string; eta: string;
}
export interface SpoolDep { id: string; name: string; note: string; status: string; ver: string }
export interface Toast { id: number; icon?: string; tone?: string; title: string; body?: string }

const RECIPES = ["3 funny shorts", "Insightful carousel", "Hot-take TikToks", "Best moment → 9:16"];

function originOf(url: string | null | undefined): string {
  const u = (url || "").toLowerCase();
  if (u.includes("youtu")) return "youtube";
  if (u.includes("instagram")) return "instagram";
  if (u.includes("tiktok")) return "tiktok";
  if (u.includes("x.com") || u.includes("twitter")) return "x";
  return "file";
}

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
      };
    });
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
    const cap = jobs.filter((j) => j.kind === "caption" || j.kind === "pipeline").at(-1);
    const win = cut?.result;
    const status = render ? "ready" : active ? (active.kind === "export" || active.kind === "pipeline" ? "rendering" : "rendering") : "queued";
    out.push({
      id: cid,
      title: "Clip " + cid.slice(0, 6),
      src: cut?.source_id || "",
      dur: win?.start != null && win?.end != null ? win.end - win.start : 0,
      aspect: (render?.result.aspect as string) || "9:16",
      style: (cap?.result.style as string) || "opus",
      platform: (render?.result.preset as string) || "tiktok",
      status, prog: active?.progress_pct ?? 0, renderId: render?.result.render_id,
    });
  }
  return out.reverse();
}

function mapJobs(snap: EventsSnapshot | null): SpoolJob[] {
  if (!snap) return [];
  const jobs: SpoolJob[] = [];
  for (const j of snap.jobs) {
    if (j.status === "downloading" || j.status === "queued" || j.status === "running")
      jobs.push({ id: j.id, type: "download", label: j.title || j.url, src: j.id, status: j.status === "downloading" ? "running" : j.status, prog: j.progress_pct, stage: j.human?.summary || "downloading", eta: j.human?.eta || "—" });
  }
  for (const t of snap.transcripts)
    if (t.status === "running" || t.status === "queued")
      jobs.push({ id: t.id, type: "transcribe", label: t.human?.summary || "transcribe", src: t.parent_job_id, status: t.status, prog: t.progress_pct, stage: "whisper", eta: "—" });
  for (const c of snap.clips)
    if (c.status === "running" || c.status === "queued")
      jobs.push({ id: c.id, type: c.kind === "moments" ? "transcribe" : "render", label: `${c.kind} · ${c.clip_id || c.source_id || ""}`, src: c.source_id || "", status: c.status, prog: c.progress_pct, stage: c.stage || c.kind, eta: "—" });
  return jobs;
}

interface SpoolCtx {
  sources: SpoolSource[];
  clips: SpoolClip[];
  jobs: SpoolJob[];
  recipes: string[];
  deps: SpoolDep[];
  nav: (screen: string, params?: { id?: string }) => void;
  agentOpen: boolean; openAgent: () => void; toggleAgent: () => void; closeAgent: () => void;
  paletteOpen: boolean; openPalette: () => void; closePalette: () => void;
  shortcutsOpen: boolean; openShortcuts: () => void; closeShortcuts: () => void;
  askAgent: (text: string) => void;
  toasts: Toast[]; pushToast: (t: Omit<Toast, "id">) => void;
}

const Ctx = createContext<SpoolCtx | null>(null);

const ROUTE: Record<string, string> = {
  home: "/", import: "/import", library: "/library", clips: "/clips", queue: "/queue",
  settings: "/settings", publish: "/publish", analytics: "/analytics",
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

  const pushToast = (t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((ts) => [...ts, { ...t, id }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), 4200);
  };

  const nav = (screen: string, params: { id?: string } = {}) => {
    if ((screen === "project" || screen === "discovery") && params.id) router.push(`/sources/${params.id}`);
    else if (["editor", "reframe", "caption"].includes(screen) && params.id) router.push(`/clips/${params.id}`);
    else router.push(ROUTE[screen] ?? "/");
  };

  const askAgent = (text: string) => {
    setAgentOpen(true);
    client.agent(text).then((r) => pushToast({ icon: "sparkles", tone: "info", title: "Agent", body: r.reply })).catch(() => {});
  };

  const deps: SpoolDep[] = doctor.data
    ? Object.entries(doctor.data.tools).map(([id, t]) => ({
        id, name: id, note: "", status: t.present ? "ok" : "missing", ver: t.version || "—",
      }))
    : [];

  const value: SpoolCtx = {
    sources: mapSources(snapshot), clips: mapClips(snapshot), jobs: mapJobs(snapshot), recipes: RECIPES, deps,
    nav, agentOpen, openAgent: () => setAgentOpen(true), toggleAgent: () => setAgentOpen((o) => !o), closeAgent: () => setAgentOpen(false),
    paletteOpen, openPalette: () => setPaletteOpen(true), closePalette: () => setPaletteOpen(false),
    shortcutsOpen, openShortcuts: () => setShortcutsOpen(true), closeShortcuts: () => setShortcutsOpen(false),
    askAgent, toasts, pushToast,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSpool(): SpoolCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error("useSpool must be used within <SpoolProvider>");
  return c;
}
