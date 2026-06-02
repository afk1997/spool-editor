import { describe, it, expect } from "vitest";
import type { EventsSnapshot, TranscriptWord } from "@spool/types";
import { mapCandidates, buildTranscript } from "@/components/spool/context";

/* Pure data-mapping logic — the layer that turns the live engine snapshot into the demo's
 * shapes. These are the highest-value units (no DOM, real branching). */

function snapshotWithMoments(): EventsSnapshot {
  return {
    ts: 0,
    jobs: [],
    transcripts: [],
    clips: [
      {
        id: "mj1", kind: "moments", source_id: "src1", clip_id: null, status: "done",
        progress_pct: 100, stage: null, elapsed_seconds: 1, params: {},
        result: {
          candidates: [
            { start: 10, end: 30, title: "A funny bit", rationale: "punchy", mode: "funny", signals: ["punchline", "reversal"] },
            { start: 40, end: 60, title: "An insight", rationale: "smart", mode: "insightful", signals: ["framework"] },
          ],
        },
        error_category: null, error_message: null, human: { summary: "" },
      },
    ],
  } as unknown as EventsSnapshot;
}

describe("mapCandidates", () => {
  it("maps a source's find_moments result into Candidate shape", () => {
    const c = mapCandidates(snapshotWithMoments(), "src1");
    expect(c).toHaveLength(2);
    expect(c[0]).toMatchObject({ title: "A funny bit", start: 10, end: 30, mode: "Funny", why: "punchy", signals: ["punchline", "reversal"], source_id: "src1", sel: true });
    expect(c[1].mode).toBe("Insightful"); // capitalized
  });

  it("fills the excerpt from transcript words inside the moment window", () => {
    const words: TranscriptWord[] = [
      { idx: 0, w: "before", start: 5, end: 5.4 },
      { idx: 1, w: "punchline", start: 12, end: 12.6 },
      { idx: 2, w: "here", start: 13, end: 13.3 },
      { idx: 3, w: "after", start: 80, end: 80.4 },
    ];
    const c = mapCandidates(snapshotWithMoments(), "src1", words);
    expect(c[0].excerpt).toBe("punchline here"); // only words inside [10,30]
  });

  it("returns [] for an unknown source or a null snapshot", () => {
    expect(mapCandidates(snapshotWithMoments(), "nope")).toEqual([]);
    expect(mapCandidates(null, "src1")).toEqual([]);
  });
});

describe("buildTranscript", () => {
  it("groups words into speaker-attributed lines", () => {
    const words: TranscriptWord[] = [
      { idx: 0, w: "Hello", start: 0, end: 0.5, speaker: "A" },
      { idx: 1, w: "there", start: 0.6, end: 1.0, speaker: "A" },
      { idx: 2, w: "Hi", start: 2.0, end: 2.3, speaker: "B" },
    ];
    const { lines, speakers } = buildTranscript(words);
    expect(lines).toHaveLength(2);
    expect(lines[0]).toMatchObject({ sp: "A", words: "Hello there" });
    expect(lines[1]).toMatchObject({ sp: "B", words: "Hi" });
    expect(Object.keys(speakers).sort()).toEqual(["A", "B"]);
  });

  it("returns empty for no words", () => {
    expect(buildTranscript(undefined)).toEqual({ lines: [], speakers: {} });
  });
});
