"use client";

import { useMemo, useRef, useState } from "react";
import type { Watch } from "@spool/api-client";
import { useSpool } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { describeActionError } from "@/lib/action-error";
import { Btn, Icon } from "@spool/ui";

/* Watches (Phase 3) — folder / channel / playlist automations. Point at a local folder or a
 * channel/playlist URL + a recipe; new videos auto-produce ranked clips into the review queue
 * (drop-a-video → review, NOT auto-published). Persisted via the engine's /watches store; "Scan
 * now" runs the real reconciler (ingest new → produce once transcribed). Zero dummy. */

const KINDS: { key: string; label: string; hint: string }[] = [
  { key: "folder", label: "Folder", hint: "A local folder — drop video files in" },
  { key: "channel", label: "Channel", hint: "A channel URL — new uploads" },
  { key: "playlist", label: "Playlist", hint: "A playlist URL — new entries" },
];

interface Form { name: string; kind: string; target: string; recipe_id: string; enabled: boolean }
const EMPTY: Form = { name: "", kind: "folder", target: "", recipe_id: "", enabled: true };
const isRemoteKind = (kind: string) => kind === "channel" || kind === "playlist";
const toForm = (w: Watch): Form => ({
  name: w.name || "", kind: w.kind || "folder", target: w.target || "",
  recipe_id: w.recipe_id || "", enabled: w.enabled ?? true,
});

export default function WatchesScreen() {
  const ctx = useSpool();
  const watchesQ = useEngineQuery((c) => c.listWatches(), []);
  const recipesQ = useEngineQuery((c) => c.listRecipes(), []);
  const watches = useMemo(() => watchesQ.data?.watches ?? [], [watchesQ.data]);
  const recipes = useMemo(() => recipesQ.data?.recipes ?? [], [recipesQ.data]);

  const [sel, setSel] = useState<string | null>(null);
  const [f, setF] = useState<Form>(EMPTY);
  const [synced, setSynced] = useState(false);
  const [busy, setBusy] = useState(false);
  const operationRef = useRef<"save" | "delete" | "scan" | null>(null);
  const remoteWatchBlock = !ctx.settingsReady || !ctx.settings
    ? ctx.settingsLoading
      ? "Checking privacy settings. Remote watch actions are disabled."
      : "Privacy settings are unavailable. Remote watch actions are disabled."
    : ctx.settings.offline
      ? "Offline mode blocks remote watch actions. Folder watches remain available."
      : null;

  if (!synced && sel === null && watches[0]) { setSel(watches[0].id); setF(toForm(watches[0])); setSynced(true); }

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((s) => ({ ...s, [k]: v }));
  const selectWatch = (w: Watch) => { if (operationRef.current) return; setSel(w.id); setF(toForm(w)); };
  const newWatch = () => { if (operationRef.current) return; setSel(null); setF(EMPTY); setSynced(true); };
  const recipeName = (id?: string) => recipes.find((r) => r.id === id)?.name || "—";
  const selWatch = watches.find((w) => w.id === sel) || null;
  const saveTouchesRemote = isRemoteKind(f.kind) || (!!selWatch && isRemoteKind(selWatch.kind));

  const save = async () => {
    if (operationRef.current || (saveTouchesRemote && remoteWatchBlock)) return;
    if (!f.name.trim() || !f.target.trim()) { ctx.pushToast({ icon: "alert", tone: "warn", title: "Name and a folder/URL are required" }); return; }
    operationRef.current = "save";
    setBusy(true);
    try {
      const body: Partial<Watch> = { name: f.name.trim(), kind: f.kind, target: f.target.trim(), recipe_id: f.recipe_id || undefined, enabled: f.enabled };
      const watch = sel ? await ctx.client.updateWatch(sel, body) : await ctx.client.createWatch(body);
      setSel(watch.id);
      watchesQ.reload();
      ctx.pushToast({ icon: "check", tone: "ok", title: "Watch saved", body: watch.name });
    } catch (error) {
      const failure = describeActionError(error);
      ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't save the watch", body: `${failure.code}: ${failure.message}` });
    } finally {
      if (operationRef.current === "save") operationRef.current = null;
      setBusy(false);
    }
  };
  const del = async () => {
    if (!sel || operationRef.current) return;
    operationRef.current = "delete";
    setBusy(true);
    try {
      await ctx.client.deleteWatch(sel);
      setSel(null);
      setF(EMPTY);
      setSynced(true);
      watchesQ.reload();
      ctx.pushToast({ icon: "trash", tone: "info", title: "Watch deleted" });
    } catch (error) {
      const failure = describeActionError(error);
      ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't delete the watch", body: `${failure.code}: ${failure.message}` });
    } finally {
      if (operationRef.current === "delete") operationRef.current = null;
      setBusy(false);
    }
  };
  const scan = async (w: Watch) => {
    if (operationRef.current || (isRemoteKind(w.kind) && remoteWatchBlock)) return;
    operationRef.current = "scan";
    setBusy(true);
    try {
      const result = await ctx.client.scanWatch(w.id);
      watchesQ.reload();
      ctx.pushToast({ icon: "eye", tone: "info", title: `Scanned “${w.name}”`,
        body: `${result.ingested.length} ingested · ${result.produced.length} produced · ${Object.keys(result.producing).length} producing · ${Object.keys(result.pending).length} transcribing · ${Object.keys(result.ingesting).length} ingesting` });
    } catch (error) {
      const failure = describeActionError(error);
      ctx.pushToast({ icon: "alert", tone: "warn", title: "Scan failed", body: `${failure.code}: ${failure.message}` });
    } finally {
      if (operationRef.current === "scan") operationRef.current = null;
      setBusy(false);
    }
  };

  const kindHint = KINDS.find((k) => k.key === f.kind)?.hint ?? "";
  const saveRemoteBlocked = saveTouchesRemote && remoteWatchBlock !== null;
  const scanRemoteBlocked = !!selWatch && isRemoteKind(selWatch.kind) && remoteWatchBlock !== null;
  const visibleRemoteBlock = saveRemoteBlocked || scanRemoteBlocked ? remoteWatchBlock : null;

  return (
    <div className="mainpad fadein" style={{ maxWidth: 1100 }}>
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Watches</div><h1 style={{ fontSize: 28 }}>Hands-off production</h1></div>
        <span className="spacer" />
        <Btn variant="ghost" icon="plus" onClick={newWatch} disabled={busy}>New watch</Btn>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", gap: 20, alignItems: "start" }}>
        {/* watch list */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {watches.length === 0 && <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No watches yet — point one at a folder or a channel/playlist + a recipe. New videos auto-produce ranked clips into the review queue.</div>}
          {watches.map((w) => (
            <button type="button" key={w.id} className="card" aria-pressed={sel === w.id} disabled={busy} onClick={() => selectWatch(w)} style={{ padding: 13, cursor: busy ? "not-allowed" : "pointer", borderColor: sel === w.id ? "var(--accent)" : "var(--line)", opacity: busy && sel !== w.id ? 0.65 : 1, width: "100%", textAlign: "left", color: "inherit", fontFamily: "inherit" }}>
              <div className="row" style={{ gap: 8, marginBottom: 5 }}>
                <span style={{ width: 7, height: 7, borderRadius: "50%", background: w.enabled ?? true ? "var(--ok)" : "var(--text-faint)" }} />
                <span style={{ fontWeight: 600, fontSize: 13.5 }}>{w.name}</span>
                <span className="spacer" />
                <span className="chip" style={{ height: 22, textTransform: "capitalize" }}>{w.kind}</span>
              </div>
              <div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{w.target}</div>
              <div className="mono" style={{ fontSize: 10.5, color: "var(--text-dim)", marginTop: 5 }}>{(w.produced?.length ?? 0)} produced · {(w.seen?.length ?? 0)} seen</div>
            </button>
          ))}
        </div>

        {/* editor */}
        <div className="card" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="row">
            <input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Watch name"
              style={{ font: "inherit", fontSize: 17, fontWeight: 600, background: "transparent", border: 0, borderBottom: "1px solid var(--line)", color: "var(--text)", outline: "none", padding: "2px 0", flex: 1 }} />
            <span className="spacer" />
            {selWatch && <Btn variant="ghost" icon="eye" onClick={() => scan(selWatch)} disabled={busy || scanRemoteBlocked} aria-describedby={scanRemoteBlocked ? "remote-watch-status" : undefined}>Scan now</Btn>}
            {sel && <button className="btn subtle sm" disabled={busy} style={{ color: "var(--err, #e5484d)" }} onClick={del} title="Delete watch"><Icon name="trash" size={14} /></button>}
            <Btn variant="primary" icon="check" onClick={save} disabled={busy || saveRemoteBlocked} aria-describedby={saveRemoteBlocked ? "remote-watch-status" : undefined}>{sel ? "Save" : "Create"}</Btn>
          </div>

          {visibleRemoteBlock && (
            <div id="remote-watch-status" role="status" className="mono" style={{ color: "var(--warn)", fontSize: 11.5 }}>
              {visibleRemoteBlock}
            </div>
          )}

          <div className="row" style={{ gap: 28, flexWrap: "wrap", alignItems: "flex-start" }}>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Kind</div>
              <div className="row" style={{ gap: 7 }}>{KINDS.map((k) => <button key={k.key} className={"chip" + (f.kind === k.key ? " solid" : "")} style={{ cursor: "pointer", height: 30 }} onClick={() => set("kind", k.key)}>{k.label}</button>)}</div>
              <div className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", marginTop: 6 }}>{kindHint}</div>
            </div>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Status</div>
              <button className={"chip" + (f.enabled ? " solid" : "")} style={{ cursor: "pointer", height: 30 }} onClick={() => set("enabled", !f.enabled)}>{f.enabled ? "Enabled" : "Paused"}</button>
            </div>
          </div>

          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>{f.kind === "folder" ? "Folder path" : "Channel / playlist URL"}</div>
            <input value={f.target} onChange={(e) => set("target", e.target.value)}
              placeholder={f.kind === "folder" ? "/Users/you/Movies/clips-in" : "https://youtube.com/@channel"}
              style={{ width: "100%", font: "inherit", fontSize: 13, padding: "8px 11px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)", fontFamily: "var(--font-mono)" }} />
          </div>

          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>Recipe</div>
            <select value={f.recipe_id} onChange={(e) => set("recipe_id", e.target.value)} style={{ width: "100%", maxWidth: 360, font: "inherit", fontSize: 13, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)" }}>
              <option value="">Choose a recipe…</option>
              {recipes.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
            {recipes.length === 0 && <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 8 }}>No recipes yet — create one on the Recipes screen first.</div>}
          </div>

          <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
            New videos are downloaded/imported, transcribed, then run through “{recipeName(f.recipe_id)}” — ranked moments are cut, reframed and captioned, landing in the Queue + Clips for review. Nothing is published automatically. “Scan now” checks immediately; set <span className="mono">SPOOL_WATCH_INTERVAL</span> to poll in the background.
          </div>
        </div>
      </div>
    </div>
  );
}
