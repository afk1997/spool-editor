import type { CSSProperties } from "react";

/* Audio-energy waveform — a centered, mirrored bar visualization (bars grow up + down from a
 * horizontal midline, the classic soundwave look) of a normalized 0..1 loudness envelope from
 * GET /sources/<id>/energy (signals.energy_envelope). Peaks ≈ louder / higher-engagement moments.
 * Used by the source "Audio energy" card (slate, grouped) and the editor timeline's Energy lane
 * (green, continuous). Real loudness data — never decorative. */
export function EnergyWave({
  bars,
  height = 60,
  color = "#7c89a8",
  groups = 1,
  groupGap = 16,
  barGap = 2,
  rounded = true,
  style,
}: {
  bars: number[];
  height?: number;
  color?: string;
  groups?: number;      // split the envelope into N clusters separated by a gap (source card = 4)
  groupGap?: number;
  barGap?: number;
  rounded?: boolean;
  style?: CSSProperties;
}) {
  if (!bars.length) return null;
  const g = Math.max(1, groups);
  const per = Math.ceil(bars.length / g);
  const chunks: number[][] = [];
  for (let i = 0; i < bars.length; i += per) chunks.push(bars.slice(i, i + per));

  return (
    <div style={{ display: "flex", alignItems: "center", gap: groupGap, height, width: "100%", ...style }}>
      {chunks.map((chunk, ci) => (
        <div key={ci} style={{ display: "flex", alignItems: "center", gap: barGap, flex: chunk.length, height: "100%" }}>
          {chunk.map((v, i) => (
            <div
              key={i}
              title={`${Math.round(v * 100)}%`}
              style={{
                flex: 1,
                minWidth: 2,
                height: `${Math.max(7, v * 100)}%`,   // centered via the row's align-items → mirrors up+down
                background: color,
                borderRadius: rounded ? 999 : 1,
                // taller (louder) bars read more solid; quiet sits back — the slate/green gradient look
                opacity: 0.32 + 0.68 * v,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
