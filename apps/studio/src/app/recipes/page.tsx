"use client";

import { useMemo, useRef, useState } from "react";
import type { Recipe } from "@spool/api-client";
import { useSpool, ENGINE_DEFAULT_WEIGHTS } from "@/components/spool/context";
import { useEngineQuery } from "@/lib/engine-context";
import { describeActionError } from "@/lib/action-error";
import { Btn, Icon } from "@spool/ui";

/* Recipes keep reusable pipeline configuration editable in Phase 0. Running a recipe depends on
 * remote moment reasoning, so its project selector and Run action remain visibly unavailable. */

const CONTENT_MODES = ["funny", "insightful", "hot-take", "story", "how-to", "q&a"];
const ASPECTS = ["9:16", "16:9", "1:1", "4:5"];
const REFRAME_MODES = ["pan", "split", "center"];
const CAPTION_PRESETS = ["opus", "karaoke", "minimal"];
const PLATFORMS = ["tiktok", "reels", "shorts", "youtube", "linkedin", "x"];
const FACTORS: { key: string; label: string }[] = [
  { key: "hook", label: "Hook" }, { key: "self_contained", label: "Self-contained" },
  { key: "arc", label: "Arc" }, { key: "energy", label: "Energy" }, { key: "length_fit", label: "Length-fit" },
  { key: "boundary_quality", label: "Boundary" },
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
  const recipesQ = useEngineQuery((c) => c.listRecipes(), []);
  const kitsQ = useEngineQuery((c) => c.listBrandKits(), []);
  const recipes = useMemo(() => recipesQ.data?.recipes ?? [], [recipesQ.data]);
  const kits = useMemo(() => kitsQ.data?.brand_kits ?? [], [kitsQ.data]);

  const [sel, setSel] = useState<string | null>(null);   // null = new (unsaved) recipe
  const [f, setF] = useState<Form>(EMPTY);
  const [synced, setSynced] = useState(false);
  const [saving, setSaving] = useState(false);
  const operationRef = useRef<"save" | "delete" | null>(null);

  // Load the first recipe into the editor once it arrives (set-state-during-render sync, once).
  if (!synced && sel === null && recipes[0]) { setSel(recipes[0].id); setF(toForm(recipes[0])); setSynced(true); }

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((s) => ({ ...s, [k]: v }));
  const selectRecipe = (r: Recipe) => { if (operationRef.current) return; setSel(r.id); setF(toForm(r)); };
  const newRecipe = () => { if (operationRef.current) return; setSel(null); setF({ ...EMPTY, weights: { ...DEFAULT_WEIGHTS } }); setSynced(true); };

  const save = async () => {
    if (operationRef.current) return;
    operationRef.current = "save";
    setSaving(true);
    try {
      const body = toRecipe(f);
      const recipe = sel ? await ctx.client.updateRecipe(sel, body) : await ctx.client.createRecipe(body);
      setSel(recipe.id);
      recipesQ.reload();
      ctx.pushToast({ icon: "check", tone: "ok", title: "Recipe saved", body: recipe.name });
    } catch (error) {
      const failure = describeActionError(error);
      ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't save the recipe", body: `${failure.code}: ${failure.message}` });
    } finally {
      if (operationRef.current === "save") operationRef.current = null;
      setSaving(false);
    }
  };
  const del = async () => {
    if (!sel || operationRef.current) return;
    operationRef.current = "delete";
    setSaving(true);
    try {
      await ctx.client.deleteRecipe(sel);
      setSel(null);
      setF({ ...EMPTY, weights: { ...DEFAULT_WEIGHTS } });
      setSynced(true);
      recipesQ.reload();
      ctx.pushToast({ icon: "trash", tone: "info", title: "Recipe deleted" });
    } catch (error) {
      const failure = describeActionError(error);
      ctx.pushToast({ icon: "alert", tone: "warn", title: "Couldn't delete the recipe", body: `${failure.code}: ${failure.message}` });
    } finally {
      if (operationRef.current === "delete") operationRef.current = null;
      setSaving(false);
    }
  };

  return (
    <div className="mainpad fadein" style={{ maxWidth: 1240 }}>
      <div className="row" style={{ marginBottom: 18 }}>
        <div><div className="eyebrow" style={{ marginBottom: 6 }}>Recipes</div><h1 style={{ fontSize: 28 }}>A saved pipeline</h1></div>
        <span className="spacer" />
        <Btn variant="ghost" icon="plus" onClick={newRecipe} disabled={saving}>New recipe</Btn>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "240px 1fr 300px", gap: 20, alignItems: "start" }}>
        {/* recipe list */}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {recipes.length === 0 && <div className="mono" style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>No recipes yet — set a content mode, aspect, caption look and platform, then Save. Running recipes is unavailable in Phase 0.</div>}
          {recipes.map((r) => (
            <button type="button" key={r.id} className="card" aria-pressed={sel === r.id} disabled={saving} onClick={() => selectRecipe(r)} style={{ padding: 13, cursor: saving ? "not-allowed" : "pointer", borderColor: sel === r.id ? "var(--accent)" : "var(--line)", opacity: saving && sel !== r.id ? 0.65 : 1, width: "100%", textAlign: "left", color: "inherit", fontFamily: "inherit" }}>
              <div style={{ fontWeight: 600, fontSize: 13.5, marginBottom: 5 }}>{r.name}</div>
              <div className="mono" style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "capitalize" }}>{summary(r)}</div>
            </button>
          ))}
        </div>

        {/* editor */}
        <div className="card" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="row">
            <input value={f.name} onChange={(e) => set("name", e.target.value)} placeholder="Recipe name"
              style={{ font: "inherit", fontSize: 17, fontWeight: 600, background: "transparent", border: 0, borderBottom: "1px solid var(--line)", color: "var(--text)", outline: "none", padding: "2px 0", flex: 1 }} />
            <span className="spacer" />
            {sel && <button className="btn subtle sm" disabled={saving} style={{ color: "var(--err, #e5484d)" }} onClick={del} title="Delete recipe"><Icon name="trash" size={14} /></button>}
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
            <select value="" disabled aria-describedby="recipe-run-status" style={{ font: "inherit", fontSize: 13, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--line-str)", background: "var(--bg-1)", color: "var(--text)" }}>
              <option value="">Choose a project…</option>
              {ctx.sources.filter((s) => s.status !== "transcribing").map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
            </select>
            <Btn variant="primary" icon="wand" disabled aria-describedby="recipe-run-status">Run recipe</Btn>
            <div id="recipe-run-status" className="mono" style={{ fontSize: 11, color: "var(--warn)", lineHeight: 1.6 }}>Remote recipe runs are unavailable in Phase 0. You can save configuration now and cut clips manually from a transcript.</div>
          </div>
        </div>
      </div>
    </div>
  );
}
