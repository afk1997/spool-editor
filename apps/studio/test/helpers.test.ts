import { describe, it, expect } from "vitest";
import { fmtDur, fmtTC, parseTC } from "@spool/ui";

describe("time formatting primitives (@spool/ui)", () => {
  it("fmtDur formats seconds as M:SS", () => {
    expect(fmtDur(0)).toBe("0:00");
    expect(fmtDur(65)).toBe("1:05");
    expect(fmtDur(600)).toBe("10:00");
  });

  it("fmtTC ⇄ parseTC round-trips on whole seconds", () => {
    expect(fmtTC(83)).toBe("01:23:00");
    expect(parseTC("01:23:00")).toBeCloseTo(83, 5);
    expect(parseTC(fmtTC(126))).toBeCloseTo(126, 5);
  });

  it("parseTC accepts MM:SS and returns NaN for malformed input", () => {
    expect(parseTC("2:00")).toBe(120);
    expect(Number.isNaN(parseTC("abc"))).toBe(true);
    expect(Number.isNaN(parseTC(""))).toBe(true);
  });
});
