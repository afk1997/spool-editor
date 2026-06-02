"use client";

import type { MomentCandidate } from "@spool/types";
import { Badge, Button, Card, fmtDuration } from "./ui";

/** A discovered moment (S5). The "glass-box" surface for Phase 1 is the matched **signals**
 *  + the rationale — named, inspectable reasons, never an opaque 0–99 (spec §5.4/§6.6).
 *  A numeric opportunity score arrives with `discover.rank` in Phase 3. */
export function CandidateCard({
  candidate,
  onCut,
  onRender,
  busy,
}: {
  candidate: MomentCandidate;
  onCut: (c: MomentCandidate) => void;
  onRender: (c: MomentCandidate) => void;
  busy?: string;
}) {
  const length = candidate.end - candidate.start;
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="font-medium text-text">{candidate.title}</h3>
        <Badge tone="neutral">{fmtDuration(length)}</Badge>
      </div>
      <p className="font-mono text-xs text-text-faint tabular-nums">
        {fmtDuration(candidate.start)} – {fmtDuration(candidate.end)}
      </p>
      {candidate.rationale && <p className="text-sm text-text-dim">{candidate.rationale}</p>}
      {candidate.signals.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {candidate.signals.map((s) => (
            <Badge key={s} tone="info">
              {s}
            </Badge>
          ))}
        </div>
      )}
      <div className="mt-1 flex gap-2">
        <Button variant="ghost" className="flex-1" disabled={!!busy} onClick={() => onCut(candidate)}>
          Cut clip
        </Button>
        <Button className="flex-1" disabled={!!busy} onClick={() => onRender(candidate)}>
          Quick render
        </Button>
      </div>
      {busy && <p className="text-xs text-accent">{busy}</p>}
    </Card>
  );
}
