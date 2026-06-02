import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
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
});
