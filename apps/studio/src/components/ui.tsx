/**
 * Small theme-token-styled primitives shared by the studio screens. Kept app-local for
 * Phase 1; the richer Design-Brief component library graduates to @spool/ui later (spec
 * §6.3). Everything here references the theme utilities (bg-bg-2, text-accent, …) — no
 * hard-coded colors — and meets the a11y bar (44px targets, visible focus, tabular-nums).
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

export function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn("rounded-lg border border-line bg-bg-2 shadow-1", className)}>{children}</div>
  );
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger";
};

export function Button({ variant = "primary", className, ...rest }: ButtonProps) {
  const base =
    "inline-flex min-h-[44px] items-center justify-center gap-2 rounded px-4 text-sm font-medium " +
    "transition-[background,opacity] duration-150 focus-visible:outline-2 focus-visible:outline-offset-2 " +
    "focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50";
  const variants = {
    primary: "bg-accent text-accent-ink hover:bg-accent-2",
    ghost: "border border-line bg-bg-1 text-text hover:bg-bg-3",
    danger: "border border-line bg-err-soft text-err hover:bg-err/10",
  };
  return <button className={cn(base, variants[variant], className)} {...rest} />;
}

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "min-h-[44px] w-full rounded border border-line bg-bg-1 px-3 text-sm text-text",
        "placeholder:text-text-faint focus-visible:outline-2 focus-visible:outline-accent",
        className,
      )}
      {...rest}
    />
  );
}

const STATUS_COLOR: Record<string, string> = {
  done: "bg-ok",
  ready: "bg-ok",
  online: "bg-ok",
  running: "bg-info",
  downloading: "bg-info",
  queued: "bg-warn",
  paused: "bg-warn",
  connecting: "bg-warn",
  error: "bg-err",
  failed: "bg-err",
  offline: "bg-err",
  cancelled: "bg-text-faint",
};

export function StatusDot({ status, pulse }: { status: string; pulse?: boolean }) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block h-2.5 w-2.5 shrink-0 rounded-full",
        STATUS_COLOR[status] ?? "bg-text-faint",
        pulse && "animate-pulse",
      )}
    />
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "ok" | "warn" | "err" | "info" }) {
  const tones = {
    neutral: "bg-bg-3 text-text-dim",
    ok: "bg-ok-soft text-ok",
    warn: "bg-warn-soft text-warn",
    err: "bg-err-soft text-err",
    info: "bg-info-soft text-info",
  };
  return (
    <span className={cn("rounded-sm px-1.5 py-0.5 text-xs font-medium tabular-nums", tones[tone])}>
      {children}
    </span>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-text-dim" role="status">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-accent" aria-hidden />
      {label}
    </div>
  );
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-line bg-bg-1 px-6 py-12 text-center">
      <p className="font-medium text-text">{title}</p>
      {hint && <p className="max-w-sm text-sm text-text-dim">{hint}</p>}
      {action}
    </div>
  );
}

export function ErrorState({ code, onRetry }: { code: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-line bg-err-soft px-6 py-10 text-center">
      <p className="font-medium text-err">Couldn&rsquo;t reach the engine</p>
      <p className="font-mono text-xs text-text-dim">{code}</p>
      {onRetry && (
        <Button variant="ghost" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

/** mm:ss / h:mm:ss from seconds. */
export function fmtDuration(seconds?: number | null): string {
  if (!seconds || seconds < 0) return "—";
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const h = Math.floor(seconds / 3600);
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}
