"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useSpool } from "./context";
import { useEngineQuery } from "@/lib/engine-context";
import { Icon } from "@spool/ui";

/* Search and navigation only. Mutating actions and future routes stay out of the palette until
 * they have a complete, failure-visible implementation. */

interface PItem { group: string; title: string; icon: string; hint?: string; run: () => void }

export function CommandPalette() {
  const ctx = useSpool();
  // Mount fresh on open so query/active start clean — no setState-in-effect reset needed.
  if (!ctx.paletteOpen) return null;
  return <PaletteInner />;
}

function PaletteInner() {
  const ctx = useSpool();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { const id = setTimeout(() => inputRef.current?.focus(), 30); return () => clearTimeout(id); }, []);

  const items = useMemo<PItem[]>(() => {
    const nav: [string, string, string][] = [["home", "Home", "home"], ["import", "Import / Paste URL", "import"], ["library", "Library", "film"], ["clips", "Clips", "scissors"], ["queue", "Render Queue", "layers"], ["brand", "Brand Kit", "palette"], ["settings", "Settings", "settings"], ["onboarding", "Dependency Doctor", "scan"]];
    const out: PItem[] = [];
    nav.forEach(([r, t, ic]) => out.push({ group: "Navigate", title: t, icon: ic, hint: "↵", run: () => ctx.nav(r) }));
    ctx.sources.slice(0, 4).forEach((s) => out.push({ group: "Sources", title: s.title, icon: "folder", run: () => ctx.nav("project", { id: s.id }) }));
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ctx.sources]);

  // Library-wide transcript search (debounced) — the demo's global search, made real:
  // every completed transcript across the library, with deep-links into the source.
  const [dq, setDq] = useState("");
  useEffect(() => { const id = setTimeout(() => setDq(q.trim()), 180); return () => clearTimeout(id); }, [q]);
  const search = useEngineQuery((c) => (dq.length >= 2 ? c.searchTranscripts(dq, { limit: 6 }) : Promise.resolve({ matches: [], returned: 0, query: dq })), [dq]);
  const hitItems = useMemo<PItem[]>(() => (search.data?.matches ?? []).map((m) => ({
    group: "Transcripts", title: m.snippet, icon: "type", hint: "↵",
    run: () => { const src = ctx.sources.find((s) => s.transcriptId === m.transcript_id); ctx.nav(src ? "project" : "library", src ? { id: src.id } : {}); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  })), [search.data]);

  const filtered = q ? items.filter((i) => i.title.toLowerCase().includes(q.toLowerCase())) : items;
  const shown = [...filtered, ...hitItems];
  const groups = shown.reduce<Record<string, PItem[]>>((a, i) => { (a[i.group] = a[i.group] || []).push(i); return a; }, {});
  const flat: PItem[] = [];
  Object.values(groups).forEach((g) => flat.push(...g));

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(flat.length - 1, a + 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
      else if (e.key === "Enter") { e.preventDefault(); if (flat[active]) { flat[active].run(); ctx.closePalette(); } }
      else if (e.key === "Escape") { ctx.closePalette(); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [flat, active]); // eslint-disable-line react-hooks/exhaustive-deps

  let idx = -1;
  return (
    <div className="overlay" onClick={ctx.closePalette}>
      <div className="palette" onClick={(e) => e.stopPropagation()}>
        <div className="psearch">
          <Icon name="search" size={18} style={{ color: "var(--text-faint)" }} />
          <input ref={inputRef} value={q} onChange={(e) => { setQ(e.target.value); setActive(0); }} placeholder="Search pages, sources, and transcripts…" />
          <span className="kbd">esc</span>
        </div>
        <div className="plist">
          {Object.entries(groups).map(([g, gi]) => (
            <div key={g}>
              <div className="pgroup">{g}</div>
              {gi.map((it) => { idx++; const a = idx; return (
                <div key={it.title} className={"pitem" + (active === a ? " active" : "")} onMouseEnter={() => setActive(a)} onClick={() => { it.run(); ctx.closePalette(); }}>
                  <span className="pico"><Icon name={it.icon} size={16} /></span><span className="pttl">{it.title}</span>{it.hint && active === a && <span className="phint kbd">↵</span>}
                </div>
              ); })}
            </div>
          ))}
          {shown.length === 0 && <div style={{ padding: "20px", textAlign: "center", color: "var(--text-faint)", fontSize: 13 }}>{search.loading ? "Searching transcripts…" : "No matching pages, sources, or transcripts."}</div>}
        </div>
      </div>
    </div>
  );
}

export function ShortcutSheet() {
  const ctx = useSpool();
  if (!ctx.shortcutsOpen) return null;
  const K = (...keys: string[]) => <span className="keys">{keys.map((k, i) => <span key={i} className="kbd">{k}</span>)}</span>;
  const groups: [string, [string, string][]][] = [
    ["Navigation", [["⌘ K", "Command palette"], ["?", "Keyboard shortcuts"], ["Esc", "Close this sheet"]]],
  ];
  return (
    <div className="overlay" onClick={ctx.closeShortcuts}>
      <div className="palette" style={{ width: "min(620px,92vw)", padding: 0 }} onClick={(e) => e.stopPropagation()}>
        <div className="psearch" style={{ padding: "15px 18px" }}>
          <Icon name="command" size={17} style={{ color: "var(--text-faint)" }} />
          <b style={{ flex: 1, fontSize: 15 }}>Keyboard shortcuts</b>
          <button className="iconbtn" aria-label="Close" onClick={ctx.closeShortcuts}><Icon name="x" size={16} /></button>
        </div>
        <div style={{ padding: "18px 20px 22px", display: "flex", flexDirection: "column", gap: 20 }}>
          {groups.map(([g, rows]) => (
            <div key={g}>
              <div className="eyebrow" style={{ marginBottom: 11 }}>{g}</div>
              <div className="kgrid">
                {rows.map(([k, d]) => <div key={d} className="krow"><span className="desc">{d}</span>{K(...k.split(" "))}</div>)}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function Toasts() {
  const ctx = useSpool();
  const ic: Record<string, string> = { ok: "var(--ok-soft)", info: "var(--info-soft)", err: "var(--err-soft)", warn: "var(--warn-soft)" };
  const cc: Record<string, string> = { ok: "var(--ok)", info: "var(--info)", err: "var(--err)", warn: "var(--warn)" };
  return (
    <div className="toast-wrap">
      {ctx.toasts.map((t) => (
        <div key={t.id} className="toast">
          <div className="ti" style={{ background: ic[t.tone || ""] || "var(--bg-3)", color: cc[t.tone || ""] || "var(--accent)" }}><Icon name={t.icon || "check"} size={16} /></div>
          <div style={{ flex: 1 }}><div style={{ fontWeight: 600, fontSize: 13.5 }}>{t.title}</div>{t.body && <div style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 2 }}>{t.body}</div>}</div>
        </div>
      ))}
    </div>
  );
}
