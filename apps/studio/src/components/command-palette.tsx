"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { cn } from "./ui";

interface Command {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

/** ⌘K command palette — keyboard-first navigation + quick actions (spec §6.1 screen set).
 *  Mounted only while open (so each open starts fresh — no reset effect); the shell owns
 *  the ⌘K shortcut. As more verbs land (find moments, render selection) they register here. */
export function CommandPalette({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);

  const go = (href: string) => {
    router.push(href);
    onClose();
  };

  const commands: Command[] = [
    { id: "home", label: "Go to Home", hint: "overview", run: () => go("/") },
    { id: "import", label: "Import a video", hint: "paste a URL", run: () => go("/import") },
    { id: "library", label: "Open Library", hint: "sources", run: () => go("/library") },
    { id: "clips", label: "Open Clips", hint: "your clips", run: () => go("/clips") },
    { id: "queue", label: "Open Render queue", hint: "in-flight work", run: () => go("/queue") },
  ];
  const filtered = commands.filter((c) => c.label.toLowerCase().includes(q.trim().toLowerCase()));
  const clampedActive = Math.min(active, Math.max(0, filtered.length - 1));

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") onClose();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(clampedActive + 1, filtered.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(clampedActive - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      filtered[clampedActive]?.run();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[12vh]"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div
        className="w-full max-w-lg overflow-hidden rounded-lg border border-line bg-bg-2 shadow-pop"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          autoFocus
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="Type a command…"
          aria-label="Command"
          className="w-full border-b border-line bg-transparent px-4 py-3.5 text-text outline-none placeholder:text-text-faint"
        />
        <ul className="max-h-80 overflow-y-auto p-1.5">
          {filtered.length === 0 ? (
            <li className="px-3 py-6 text-center text-sm text-text-faint">No matching command</li>
          ) : (
            filtered.map((c, i) => (
              <li key={c.id}>
                <button
                  onClick={c.run}
                  onMouseEnter={() => setActive(i)}
                  className={cn(
                    "flex w-full items-center justify-between rounded px-3 py-2.5 text-left text-sm",
                    i === clampedActive ? "bg-accent-soft text-accent" : "text-text hover:bg-bg-3",
                  )}
                >
                  <span>{c.label}</span>
                  {c.hint && <span className="text-xs text-text-faint">{c.hint}</span>}
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}
