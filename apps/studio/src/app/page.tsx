"use client";

import Link from "next/link";
import { useEngineQuery, useLive } from "@/lib/engine-context";
import { Badge, Card, Spinner, StatusDot, cn } from "@/components/ui";

/** S1 Home + S0 Dependency-Doctor. Live engine status, the real tool/encoder probe, the
 *  feature registry, and quick entry points — every value from api_v1, zero mock. */
export default function Home() {
  const { connection, snapshot } = useLive();
  const doctor = useEngineQuery((c) => c.doctor());
  const caps = useEngineQuery((c) => c.capabilities());

  const jobs = snapshot?.jobs ?? [];
  const clips = snapshot?.clips ?? [];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      <header className="space-y-1">
        <h1 className="font-display text-3xl font-semibold tracking-tight">Spool</h1>
        <p className="text-text-dim">
          Local-first clip studio — turn long videos into platform-ready vertical clips,
          entirely on your machine.
        </p>
      </header>

      {/* engine connection */}
      <Card className="flex items-center gap-3 p-4">
        <StatusDot status={connection} pulse={connection === "connecting"} />
        <span className="font-medium">
          {connection === "online" ? "Engine connected" : connection === "connecting" ? "Connecting…" : "Engine offline"}
        </span>
        {connection === "offline" && (
          <code className="ml-auto rounded bg-bg-3 px-2 py-1 font-mono text-xs text-text-dim">cd engine &amp;&amp; ./trove.sh</code>
        )}
      </Card>

      {/* dependency doctor (S0) */}
      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold text-text-dim">Dependencies</h2>
        <Card className="p-4">
          {doctor.loading ? (
            <Spinner label="Probing tools…" />
          ) : doctor.error ? (
            <p className="text-sm text-err">Couldn&rsquo;t probe (<span className="font-mono">{doctor.error}</span>)</p>
          ) : (
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4">
              {Object.entries(doctor.data!.tools).map(([name, t]) => (
                <div key={name} className="flex items-center gap-2">
                  <StatusDot status={t.present ? "done" : "error"} />
                  <span className="text-sm text-text">{name}</span>
                  <span className="ml-auto font-mono text-xs text-text-faint">{t.version ?? "—"}</span>
                </div>
              ))}
            </div>
          )}
          {doctor.data && (
            <p className="mt-3 border-t border-line pt-3 text-xs text-text-dim">
              {doctor.data.encoders.length} hardware encoder{doctor.data.encoders.length === 1 ? "" : "s"}:{" "}
              <span className="font-mono">{doctor.data.encoders.join(", ") || "x264 (software)"}</span>
            </p>
          )}
        </Card>
      </section>

      {/* feature registry */}
      {caps.data && (
        <div className="flex flex-wrap gap-2">
          <Badge tone={caps.data.features.diarization ? "ok" : "neutral"}>
            diarization {caps.data.features.diarization ? "on" : "off"}
          </Badge>
          <Badge tone={caps.data.features.clips ? "ok" : "neutral"}>clips</Badge>
          <Badge tone="info">schema v{caps.data.schema_version}</Badge>
          {caps.data.auth_required && <Badge tone="warn">auth required</Badge>}
        </div>
      )}

      {/* quick entry points */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <QuickLink href="/import" title="Import" body="Paste a URL to download + transcribe." />
        <QuickLink href="/library" title="Library" body={`${jobs.filter((j) => j.status === "done").length} sources ready.`} />
        <QuickLink href="/queue" title="Queue" body={`${clips.length} renders, ${jobs.length} downloads.`} />
      </div>
    </div>
  );
}

function QuickLink({ href, title, body }: { href: string; title: string; body: string }) {
  return (
    <Link href={href} className={cn("group rounded-lg border border-line bg-bg-2 p-4 shadow-1", "transition-colors hover:border-line-str")}>
      <p className="font-medium text-text group-hover:text-accent">{title} →</p>
      <p className="mt-1 text-sm text-text-dim">{body}</p>
    </Link>
  );
}
