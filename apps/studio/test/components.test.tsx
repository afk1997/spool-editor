import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Chip, Progress } from "@spool/ui";
import { CandidateCard } from "@/components/spool/work";
import type { Candidate } from "@/components/spool/context";

describe("@spool/ui primitives", () => {
  it("Chip renders its children", () => {
    render(<Chip tone="acc">ready</Chip>);
    expect(screen.getByText("ready")).toBeInTheDocument();
  });

  it("Progress clamps the bar width to 0–100%", () => {
    const { container } = render(<Progress value={150} />);
    const fill = container.querySelector(".bar > i") as HTMLElement;
    expect(fill.style.width).toBe("100%");
  });
});

describe("CandidateCard (glass-box = real signals + excerpt)", () => {
  const c: Candidate = { id: "c1", title: "A funny bit", start: 10, end: 30, mode: "Funny", why: "a punchy reason", excerpt: "the actual transcript line", signals: ["punchline", "reversal"], sel: false, source_id: "s1" };

  it("renders the title, rationale, and excerpt", () => {
    render(<CandidateCard c={c} selected={false} onToggle={() => {}} />);
    expect(screen.getByText("A funny bit")).toBeInTheDocument();
    expect(screen.getByText("WHY THIS WORKS")).toBeInTheDocument();
    expect(screen.getByText("a punchy reason")).toBeInTheDocument();
    expect(screen.getByText(/the actual transcript line/)).toBeInTheDocument();
  });

  it("shows Accept when unselected and Selected when selected", () => {
    const { rerender } = render(<CandidateCard c={c} selected={false} onToggle={() => {}} />);
    expect(screen.getByText("Accept")).toBeInTheDocument();
    rerender(<CandidateCard c={c} selected onToggle={() => {}} />);
    expect(screen.getByText("Selected")).toBeInTheDocument();
  });

  const scored: Candidate = {
    ...c, id: "c2", score: 71,
    factors: { hook: 0.71, self_contained: 0.55, arc: 0.36, energy: 0.52, length_fit: 1.0 },
    weights: { hook: 0.3, self_contained: 0.25, arc: 0.15, energy: 0.2, length_fit: 0.1 },
  };

  it("shows the glass-box score and expands to NAMED factor bars that reflect each value", () => {
    render(<CandidateCard c={scored} selected={false} onToggle={() => {}} />);
    expect(screen.getByText("71")).toBeInTheDocument();                 // the headline score
    fireEvent.click(screen.getByTitle(/factors/i));                     // expand the breakdown
    for (const label of ["Hook", "Self-contained", "Arc", "Energy", "Length-fit"])
      expect(screen.getByText(label)).toBeInTheDocument();              // every score traces to a named factor
    // The bars aren't decorative: each factor value renders as its own percentage. Use
    // collision-free values (arc 0.36, self_contained 0.55) so neither clashes with the 71 headline.
    expect(screen.getByText("36")).toBeInTheDocument();                 // arc 0.36 → 36
    expect(screen.getByText("55")).toBeInTheDocument();                 // self_contained 0.55 → 55
  });

  it("prefers the reweighted dynScore over the default score when ranking", () => {
    render(<CandidateCard c={scored} selected={false} onToggle={() => {}} dynScore={42} />);
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.queryByText("71")).not.toBeInTheDocument();
  });
});

import { WindowList } from "@/components/spool/virtual";

describe("WindowList", () => {
  it("subscribes to the .main scroll container, not the window", async () => {
    const main = document.createElement("div");
    main.className = "main";
    document.body.appendChild(main);
    const spy = vi.spyOn(main, "addEventListener");
    const winSpy = vi.spyOn(window, "addEventListener");
    const host = document.createElement("div");
    main.appendChild(host);
    render(
      <WindowList items={Array.from({ length: 200 }, (_, i) => i)} getKey={(i) => i}>
        {(i) => <div>{`row ${i}`}</div>}
      </WindowList>,
      { container: host },
    );
    await new Promise((r) => setTimeout(r, 0)); // let the mount effect resolve .main
    const mainScroll = spy.mock.calls.some(([ev]) => ev === "scroll");
    const windowScroll = winSpy.mock.calls.some(([ev]) => ev === "scroll");
    expect(mainScroll).toBe(true);
    expect(windowScroll).toBe(false);
    spy.mockRestore();
    winSpy.mockRestore();
    main.remove();
  });
});
