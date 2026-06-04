"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import type { Recipe } from "@spool/api-client";
import { useSpool, ENGINE_DEFAULT_WEIGHTS } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { Btn, Icon } from "@spool/ui";

/* Recipes (Phase 3) — a saved end-to-end pipeline: the reusable decisions (content mode + count,
 * optional ranking weights, and the render settings) that drive render.pipeline. Persisted via the
 * engine's /recipes store; "Run on a project" applies the recipe end-to-end (find → rank → top-N →
 * render pipeline per moment) → ranked clips in the review queue. The unattended drop-a-video→
 * auto-clips→review flow is the watch-folder step. Zero dummy — every control calls the real engine. */

const CONTENT_MODES = ["funny", "insightful", "hot-take", "story", "how-to", "q&a"];
const ASPECTS = ["9:16", "16:9", "1:1", "4:5"];
const REFRAME_MODES = ["pan", "split", "center"];
const CAPTION_PRESETS = ["opus", "karaoke", "minimal"];
const PLATFORMS = ["tiktok", "reels", "shorts", "youtube", "linkedin", "x"];
const FACTORS: { key: string; label: string }[] = [
  { key: "hook", label: "Hook" }, { key: "self_contained", label: "Self-contained" },
  { key: "arc", label: "Arc" }, { key: "energy", label: "Energy" }, { key: "length_fit", label: "Length-fit" },
];

interface Form {
  name: string; content_mode: string; count: number; aspect: string; reframe_mode: string;
  caption_preset: string; platform: string; fast: boolean; brand_kit_id: string; weights: Record<string, number>;
}
// Seed the ranking-weight sliders from the engine's DEFAULT_WEIGHTS (integer ratios that normalize
// to the engine's .30/.25/.20/.15/.10), so a fresh recipe ranks the way the engine would by default.
const DEFAULT_WEIGHTS = ENGINE_DEFAULT_WEIGHTS;
const EMPTY: Form = {
  name: "", content_mode: "funny", count: 6, aspect: "9:16", reframe_mode: "pan",
  caption_preset: "opus", platform: "tiktok", fast: true, brand_kit_id: "", weights: { ...DEFAULT_WEIGHTS },
};
const toForm = (r: Recipe): Form => ({
  name: r.name || "", content_mode: r.content_mode || "funny", count: r.count ?? 6, aspect: r.aspect || "9:16",
  reframe_mode: r.reframe_mode || "pan", caption_preset: r.caption_preset || "opus", platform: r.platform || "tiktok",
  fast: r.fast ?? true, brand_kit_id: r.brand_kit_id || "", weights: { ...DEFAULT_WEIGHTS, ...(r.weights || {}) },
});
const toRecipe = (f: Form): Partial<Recipe> => ({
  name: f.name.trim() || "Untitled recipe", content_mode: f.content_mode, count: f.count, aspect: f.aspect,
  reframe_mode: f.reframe_mode, caption_preset: f.caption_preset, platform: f.platform, fast: f.fast,
  brand_kit_id: f.brand_kit_id || undefined, weights: f.weights,
});
const summary = (r: Recipe) => [r.content_mode, r.aspect, r.platform].filter(Boolean).join(" · ");

function Chips({ options, value, onPick }: { options: string[]; value: string; onPick: (v: string) => void }) {
  return (
    <div className="row" style={{ gap: 7, flexWrap: "wrap" }}>
      {options.map((o) => (
        <button key={o} className={"chip" + (value === o ? " solid" : "")} style={{ cursor: "pointer", height: 30, textTransform: "capitalize" }} onClick={() => onPick(o)}>{o}</button>
      ))}
    </div>
  );
}

export default function RecipesScreen() {
  const ctx = useSpool();
  const router = useRouter();
  const recipesQ = useEngineQuery((c) => c.listRecipes(), []);
  const kitsQ = useEngineQuery((c) => c.listBrandKits(), []);
  const recipes = useMemo(() => recipesQ.data?.recipes ?? [], [recipesQ.data]);
  const kits = useMemo(() => kitsQ.data?.brand_kits ?? [], [kitsQ.data]);

  const [sel, setSel] = useState<string | null>(null);   // null = new (unsaved) recipe
  const [f, setF] = useState<Form>(EMPTY);
  const [synced, setSynced] = useState(false);
  const [runSrc, setRunSrc] = useState("");
  const [saving, setSaving] = useState(false);

  // Load the first recipe into the editor once it arrives (set-state-during-render sync, once).
  if (!synced && sel === null && recipes.length) { setSel(recipes[0].id); setF(toForm(recipes[0])); setSynced(true); }

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((s) => ({ ...s, [k]: v }));
  const selectRecipe = (r: Recipe) => { setSel(r.id); setF(toForm(r)); };
  const newRecipe = () => { setSel(null); setF({ ...EMPTY, weights: { ...DEFAULT_WEIGHTS } }); setSynced(true); };

  const save = () => {
    setSaving(true);
    const body = toRecipe(f);
    const p = sel ? ctx.client.updateRecipe(sel, body) : ctx.client.createRecipe(body);
    p.then((r) => { setSel(r.id); recipesQ.reload(); ctx.pushToast({ icon: "check", tone: "ok", title: "Recipe saved", body: r.name }); })
      .catch(() => ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't save the recipe" }))
      .finally(() => setSaving(false));
  };
  const del = () => {
    if (!sel) return;
    ctx.client.deleteRecipe(sel).then(() => { newRecipe(); recipesQ.reload(); ctx.pushToast({ icon: "trash", tone: "info", title: "Recipe deleted" }); }).catch(() => ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't delete the recipe" }));
  };

  // Run a recipe on a project: apply it END-TO-END — find → glass-box rank → top-N → a render
  // pipeline per moment with the recipe's aspect/reframe/caption/brand-kit/platform → the review
  // queue (the same /produce the watch's "Scan now" runs). A saved recipe runs by id (provenance);
  // an unsaved draft runs inline so all its settings still ride along.
  // A saved recipe runs by id, so the toast + sidebar copy must describe the SAVED recipe — not the
  // unsaved editor form, which may have drifted from what's actually being produced. An unsaved
  // draft runs inline, so it's the form that rides along.
  const ranSel = sel ? recipes.find((r) => r.id === sel) : null;
  const runName = ranSel?.name ?? f.name, runCount = ranSel?.count ?? f.count, runMode = ranSel?.content_mode ?? f.content_mode;

  const run = () => {
    if (!runSrc) return;
    const body = sel ? { recipe_id: sel } : toRecipe(f);
    ctx.client.produce(runSrc, body)
      .then(() => { ctx.pushToast({ icon: "wand", tone: "info", title: `Running “${runName || "recipe"}”`, body: `Producing ${runCount} ranked ${runMode} clips — they'll land in the review queue.` }); router.push(`/queue`); })
      .catch(() => ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't run — is the source transcribed?" }));
  };

  return (
    <div className="mainpad fadein" style={{ maxWidth: 1240 }}>
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Recipes</div><h1 style={{ fontSize: 28 }}>A saved pipeline</h1></div>
        <span className="spacer" />
        <Btn variant="ghost" icon="plus" onClick={newRecipe}>New recipe</Btn>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "240px 1fr 300px", gap: 20, alignItems: "start" }}>
        {/* recipe list */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {recipes.length === 0 && <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No recipes yet — set a content mode, aspect, caption look and platform, then Save. Run it on any project to produce ranked clips.</div>}
          {recipes.map((r) => (
            <div key={r.id} className="card" onClick={() => selectRecipe(r)} style={{ padding: 13, cursor: "pointer", borderColor: sel === r.id ? "var(--accent)" : "var(--line)" }}>
              <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 5 }}>{r.name}</div>
              <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "capitalize" }}>{summary(r)}</div>
            </div>
          ))}
        </div>

        {/* editor */}
        <div className="card" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="row">
            <input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Recipe name"
              style={{ font: "inherit", fontSize: 17, fontWeight: 600, background: "transparent", border: 0, borderBottom: "1px solid var(--line)", color: "var(--text)", outline: "none", padding: "2px 0", flex: 1 }} />
            <span className="spacer" />
            {sel && <button className="btn subtle sm" style={{ color: "var(--err, #e5484d)" }} onClick={del} title="Delete recipe"><Icon name="trash" size={14} /></button>}
            <Btn variant="primary" icon="check" onClick={save} disabled={saving}>{sel ? "Save" : "Create"}</Btn>
          </div>

          <div className="row" style={{ gap: 28, flexWrap: "wrap" }}>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Find moments — mode</div><Chips options={CONTENT_MODES} value={f.content_mode} onPick={(v) => set("content_mode", v)} /></div>
            <div style={{ minWidth: 180 }}>
              <div className="row" style={{ marginBottom: 8 }}><span className="eyebrow">Count</span><span className="spacer" /><span className="mono" style={{ fontSize: 11, color: "var(--text-dim)" }}>{f.count}</span></div>
              <input type="range" min={1} max={20} value={f.count} onChange={(e) => set("count", +e.target.value)} style={{ width: "100%", accentColor: "var(--accent)" }} aria-label="Moment count" />
            </div>
          </div>

          <div className="row" style={{ gap: 28, flexWrap: "wrap" }}>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Aspect</div><Chips options={ASPECTS} value={f.aspect} onPick={(v) => set("aspect", v)} /></div>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Reframe</div><Chips options={REFRAME_MODES} value={f.reframe_mode} onPick={(v) => set("reframe_mode", v)} /></div>
          </div>

          <div className="row" style={{ gap: 28, flexWrap: "wrap" }}>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Caption preset</div><Chips options={CAPTION_PRESETS} value={f.caption_preset} onPick={(v) => set("caption_preset", v)} /></div>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Platform</div><Chips options={PLATFORMS} value={f.platform} onPick={(v) => set("platform", v)} /></div>
          </div>

          <div className="row" style={{ gap: 28, flexWrap: "wrap", alignItems: "flex-end" }}>
            <div><div className="eyebrow" style={{ marginBottom: 8 }}>Encode</div>
              <div className="row" style={{ gap: 7 }}>
                <button className={"chip" + (f.fast ? " solid" : "")} style={{ cursor: "pointer", height: 30 }} onClick={() => set("fast", true)}>Fast</button>
                <button className={"chip" + (!f.fast ? " solid" : "")} style={{ cursor: "pointer", height: 30 }} onClick={() => set("fast", false)}>Quality</button>
              </div>
            </div>
            <div style={{ flex: 1, minWidth: 200 }}><div className="eyebrow" style={{ marginBottom: 6 }}>Brand kit (optional)</div>
              <select value={f.brand_kit_id} onChange={(e) => set("brand_kit_id", e.target.value)} style={{ width: "100%", font: "inherit", fontSize: 13, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)" }}>
                <option value="">None</option>
                {kits.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
              </select>
            </div>
          </div>

          <div>
            <div className="row" style={{ marginBottom: 10 }}><span className="eyebrow">Ranking weights</span><span className="spacer" /><span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>glass-box — prioritize the moments to surface</span></div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 14 }}>
              {FACTORS.map((m) => (
                <div key={m.key}>
                  <div className="row" style={{ marginBottom: 6 }}><span style={{ fontSize: 11.5, fontWeight: 600 }}>{m.label}</span><span className="spacer" /><span className="mono" style={{ fontSize: 10.5, color: "var(--text-dim)" }}>{f.weights[m.key]}×</span></div>
                  <input type="range" min={0} max={6} value={f.weights[m.key]} onChange={(e) => set("weights", { ...f.weights, [m.key]: +e.target.value })} style={{ width: "100%", accentColor: "var(--accent)" }} aria-label={m.label + " weight"} />
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* run on a project */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
            <div className="eyebrow">Run on a project</div>
            <select value={runSrc} onChange={(e) => setRunSrc(e.target.value)} style={{ font: "inherit", fontSize: 13, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)" }}>
              <option value="">Choose a project…</option>
              {ctx.sources.filter((s) => s.status !== "transcribing").map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
            </select>
            <Btn variant="primary" icon="wand" onClick={run} disabled={!runSrc}>Run recipe</Btn>
            <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.6 }}>Produces {runCount} ranked {runMode} clips end-to-end with this recipe&apos;s reframe, captions and platform — they land in the review queue. Unattended drop-a-video→review is the watch-folder step.</div>
          </div>
        </div>
      </div>
    </div>
  );
}
