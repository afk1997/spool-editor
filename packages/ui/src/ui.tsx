/* Spool icon set + shared primitives — a faithful TS port of the approved demo's ui.jsx.
 * Same class names + SVG paths so spool.css styles them identically. Used by every screen. */
import type { CSSProperties, ReactNode, ButtonHTMLAttributes } from "react";

export const ICONS: Record<string, string> = {
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM21 21l-4.3-4.3",
  command: "M8 5a2 2 0 1 0-2 2h12a2 2 0 1 0-2-2v12a2 2 0 1 0 2-2H6a2 2 0 1 0 2 2V5Z",
  home: "M3 10.5 12 3l9 7.5M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5",
  import: "M12 3v12M7 10l5 5 5-5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2",
  film: "M4 4h16v16H4zM4 9h16M4 15h16M9 4v16M15 4v16",
  scissors: "M6 6a2.5 2.5 0 1 0 3.5 3.5L20 20M6 18a2.5 2.5 0 1 1 3.5-3.5L20 4M9.5 9.5 12 12",
  layers: "M12 3 3 8l9 5 9-5-9-5ZM3 13l9 5 9-5M3 17l9 5 9-5",
  send: "M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z",
  chart: "M4 20V10M10 20V4M16 20v-8M22 20H2",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 2.9-1.2V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z",
  help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01",
  play: "M6 4l14 8-14 8V4Z",
  pause: "M7 4h3v16H7zM14 4h3v16h-3z",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18M6 6l12 12",
  plus: "M12 5v14M5 12h14",
  alert: "M12 3 2 20h20L12 3ZM12 9v5M12 17h.01",
  link: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5ZM14 3v5h5",
  upload: "M12 17V5M7 10l5-5 5 5M5 19h14",
  sparkles: "M12 3l1.8 4.7L18.5 9.5 13.8 11.3 12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3ZM19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14Z",
  terminal: "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1ZM7 9l3 3-3 3M13 15h4",
  trash: "M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13",
  pen: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7ZM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  refresh: "M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5",
  arrowR: "M5 12h14M13 6l6 6-6 6",
  dots: "M5 12h.01M12 12h.01M19 12h.01",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  shield: "M12 3 4 6v6c0 5 3.5 7.5 8 9 4.5-1.5 8-4 8-9V6l-8-3ZM9 12l2 2 4-4",
  cpu: "M6 6h12v12H6zM9 9h6v6H9M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2",
  drive: "M4 4h16a1 1 0 0 1 1 1v6H3V5a1 1 0 0 1 1-1ZM3 11h18v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-8ZM7 15.5h.01",
  mic: "M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3ZM5 11a7 7 0 0 0 14 0M12 18v3",
  type: "M4 7V5h16v2M9 5v14M9 19h6",
  palette: "M12 21a9 9 0 1 1 0-18c5 0 9 3.6 9 8 0 2.5-2 3.5-3.5 3.5H15a2 2 0 0 0-1.5 3.3A1.5 1.5 0 0 1 12 21ZM7.5 11a1 1 0 1 0 0-2 1 1 0 0 0 0 2ZM12 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2ZM16.5 11a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z",
  crop: "M6 2v14a2 2 0 0 0 2 2h14M2 6h14a2 2 0 0 1 2 2v14",
  frame: "M4 7h16M4 17h16M7 4v16M17 4v16M4 4h16v16H4z",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7v5l3 2",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z",
  zap: "M13 2 4 14h7l-1 8 9-12h-7l1-8Z",
  expand: "M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3",
  arrowL: "M19 12H5M11 6l-6 6 6 6",
  chevD: "M6 9l6 6 6-6",
  chevR: "M9 6l6 6-6 6",
  star: "M12 3l2.6 6.3 6.8.5-5.2 4.4 1.6 6.6L12 17.8 6.2 21.4l1.6-6.6L2.6 9.8l6.8-.5L12 3Z",
  download: "M12 3v12M7 10l5 5 5-5M4 19h16",
  scan: "M4 7V5a1 1 0 0 1 1-1h2M17 4h2a1 1 0 0 1 1 1v2M20 17v2a1 1 0 0 1-1 1h-2M7 20H5a1 1 0 0 1-1-1v-2M4 12h16",
  spinner: "M12 3a9 9 0 1 0 9 9",
  message: "M21 11.5a8.5 8.5 0 0 1-12.6 7.4L3 21l2.1-5.4A8.5 8.5 0 1 1 21 11.5Z",
  wand: "M15 4V2M15 10V8M11 6H9M21 6h-2M18 9l-1.5-1.5M18 3l-1.5 1.5M4 20l9-9M13 7l1.5 1.5",
  undo: "M9 14 4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3",
  volume: "M11 5 6 9H2v6h4l5 4V5ZM15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14",
  chevL: "M15 6l-6 6 6 6",
  minus: "M5 12h14",
  bolt: "M13 2 4 14h7l-1 8 9-12h-7l1-8Z",
  pin: "M12 21s-6-5.7-6-10a6 6 0 1 1 12 0c0 4.3-6 10-6 10ZM12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z",
  filter: "M3 5h18l-7 8v6l-4 2v-8L3 5Z",
  flip: "M3 8h12l-3-3M21 16H9l3 3M3 8v0M3 8a9 9 0 0 1 9-5M21 16a9 9 0 0 1-9 5",
  music: "M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
  globe: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18Z",
  layout: "M3 4h18v16H3zM3 10h18M9 10v10",
  slash: "M7 17 17 7",
  copy: "M9 9h11a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1ZM5 15H4a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h11a1 1 0 0 1 1 1v1",
};

type IconProps = { name: string; size?: number; stroke?: number; fill?: string; style?: CSSProperties } & Record<string, unknown>;
export function Icon({ name, size = 18, stroke = 1.7, fill, style, ...rest }: IconProps) {
  const d = ICONS[name];
  const solid = name === "play" || name === "pause";
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={solid ? "currentColor" : fill || "none"}
      stroke={solid ? "none" : "currentColor"} strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round"
      style={style} {...rest}>
      <path d={d} />
    </svg>
  );
}

export function SpoolMark({ size = 40 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.7"
      strokeLinecap="round" strokeLinejoin="round" style={{ display: "block" }}>
      <rect x="6" y="4.5" width="20" height="3.6" rx="1.8" />
      <rect x="6" y="23.9" width="20" height="3.6" rx="1.8" />
      <path d="M9.3 8.1 V23.9 M22.7 8.1 V23.9" />
      <path d="M9.3 12 H22.7 M9.3 19.9 H22.7" opacity="0.55" />
      <path d="M9.3 16 H22.7" stroke="var(--accent)" strokeWidth="2.2" opacity="1" />
    </svg>
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: string; size?: string; icon?: string; iconR?: string; children?: ReactNode;
};
export function Btn({ variant = "", size = "", icon, iconR, children, ...rest }: BtnProps) {
  return (
    <button className={`btn ${variant} ${size}`.trim()} {...rest}>
      {icon && <Icon name={icon} size={size === "sm" ? 15 : 17} />}
      {children}
      {iconR && <Icon name={iconR} size={size === "sm" ? 15 : 17} />}
    </button>
  );
}

export function Chip({ tone = "", dot = false, children }: { tone?: string; dot?: boolean; children: ReactNode }) {
  return <span className={`chip ${tone} ${dot ? "dot" : ""}`.trim()}>{children}</span>;
}

export function Progress({ value = 0, tone = "", striped = false }: { value?: number; tone?: string; striped?: boolean }) {
  return (
    <div className={`bar ${tone} ${striped ? "striped" : ""}`.trim()}>
      <i style={{ width: Math.max(0, Math.min(100, value)) + "%" }} />
    </div>
  );
}

export function Ring({ value = 0, size = 34, w = 3 }: { value?: number; size?: number; w?: number }) {
  const r = (size - w) / 2, c = 2 * Math.PI * r;
  return (
    <svg className="ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle className="track" cx={size / 2} cy={size / 2} r={r} strokeWidth={w} />
      <circle className="fill" cx={size / 2} cy={size / 2} r={r} strokeWidth={w} strokeDasharray={c} strokeDashoffset={c * (1 - value / 100)} />
    </svg>
  );
}

export function Switch({ on, onClick, label, disabled = false }: { on?: boolean; onClick?: () => void; label: string; disabled?: boolean }) {
  return <button type="button" role="switch" aria-checked={!!on} aria-label={label} disabled={disabled} className={"switch" + (on ? " on" : "")} onClick={onClick}><i /></button>;
}

type SegOpt = string | { value: string; label: string; icon?: string; ariaLabel?: string };
export function Seg({ value, onChange, options, neutral = false, disabled = false }: { value: string; onChange: (v: string) => void; options: SegOpt[]; neutral?: boolean; disabled?: boolean }) {
  return (
    <div className={"seg" + (neutral ? " neutral" : "")}>
      {options.map((o) => {
        const val = typeof o === "string" ? o : o.value;
        const lbl = typeof o === "string" ? o : o.label;
        const ic = typeof o === "string" ? undefined : o.icon;
        const ariaLabel = typeof o === "string" ? undefined : o.ariaLabel;
        return <button type="button" key={val} className={value === val ? "on" : ""} aria-label={ariaLabel} aria-pressed={value === val} disabled={disabled} onClick={() => onChange(val)}>{ic && <Icon name={ic} size={14} />}{lbl}</button>;
      })}
    </div>
  );
}

export function Empty({ icon = "sparkles", title, children, action }: { icon?: string; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="ill"><Icon name={icon} size={32} /></div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function Stat({ v, l }: { v: ReactNode; l: ReactNode }) {
  return <div className="stat"><div className="v tnum">{v}</div><div className="l">{l}</div></div>;
}

export function AspectBadge({ a = "9:16" }: { a?: string }) {
  return <span className="badge mono">{a}</span>;
}

export const fmtDur = (s: number) => { s = Math.round(s); const m = Math.floor(s / 60), ss = s % 60; return `${m}:${String(ss).padStart(2, "0")}`; };
export const fmtTC = (s: number) => { const m = Math.floor(s / 60), ss = Math.floor(s % 60), f = Math.floor((s % 1) * 30); return `${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}:${String(f).padStart(2, "0")}`; };
/** Inverse of fmtTC — parse "MM:SS:FF" (or "MM:SS") back to seconds; NaN if malformed. */
export const parseTC = (tc: string): number => {
  const parts = tc.trim().split(":").map((p) => parseInt(p, 10));
  if (!parts.length || parts.some((n) => Number.isNaN(n))) return NaN;
  const [m = 0, s = 0, f = 0] = parts;
  return m * 60 + s + f / 30;
};

/* deterministic hue from a string (video-frame placeholder thumbnail) */
function seedHue(s: string) { let h = 0; for (let i = 0; i < (s || "").length; i++) h = (h * 31 + s.charCodeAt(i)) % 360; return h; }

export function Thumb({ seed = "", kind = "talking-head", vertical = false, children, label }: { seed?: string; kind?: string; vertical?: boolean; children?: ReactNode; label?: false }) {
  const h = seedHue(seed);
  const bg = `radial-gradient(125% 100% at 32% 16%, oklch(0.355 0.022 ${h}) 0%, oklch(0.215 0.015 ${h}) 52%, oklch(0.14 0.009 ${h}) 100%)`;
  return (
    <div className={"thumb" + (vertical ? " v" : "")}>
      <div className="ph" style={{ background: bg }} />
      <div className="ph" style={{ background: "repeating-linear-gradient(115deg, rgba(255,255,255,0.035) 0 2px, transparent 2px 9px)" }} />
      <div className="ph" style={{ background: "radial-gradient(80% 60% at 50% 38%, rgba(0,0,0,0) 40%, rgba(0,0,0,0.42) 100%)" }} />
      {label !== false && <div style={{ position: "absolute", left: 0, right: 0, top: "50%", transform: "translateY(-50%)", textAlign: "center", fontFamily: "var(--font-mono)", fontSize: 10.5, color: "rgba(255,255,255,0.5)", letterSpacing: ".04em" }}>{kind}</div>}
      <div className="grad" />
      {children}
    </div>
  );
}

const FILE_GLYPH = { c: "var(--text-dim)", label: "FILE" };
const SOURCE_GLYPH: Record<string, { c: string; label: string }> = {
  youtube: { c: "#FF3B30", label: "YT" },
  instagram: { c: "#E0529C", label: "IG" },
  tiktok: { c: "#25F4EE", label: "TT" },
  file: FILE_GLYPH,
  x: { c: "#fff", label: "X" },
};
export function SourceGlyph({ type = "file" }: { type?: string }) {
  const g = SOURCE_GLYPH[type] ?? FILE_GLYPH;
  return <span className="badge" style={{ color: g.c, background: "rgba(0,0,0,0.62)", fontWeight: 700 }}>{g.label}</span>;
}
