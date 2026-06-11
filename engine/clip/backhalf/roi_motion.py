# Spool engine · clip back-half primitive (vendored, adapted from an MIT-licensed
# upstream). Algorithm preserved; Spool patched the hardcoded-30fps timeline (uses
# parsed pts_time) and added empty/mismatch guards — see docs/CODE_REVIEW.md §3.2.
# Full license + attribution live in THIRD_PARTY_LICENSES.md at the repo root.
#!/usr/bin/env python3
"""Build speaker timeline from two ROI motion files.

Usage: analyze.py LEFT_MOTION.txt RIGHT_MOTION.txt [MIN_DUR]
Stdout: JSON segments. Stderr: count summary.
"""
import re, json, sys

def parse(path):
    times, vals = [], []
    cur_t = None
    with open(path) as f:
        for line in f:
            m = re.match(r"frame:\d+\s+pts:\d+\s+pts_time:([0-9.]+)", line)
            if m:
                cur_t = float(m.group(1)); continue
            m = re.match(r"lavfi\.signalstats\.YAVG=([0-9.]+)", line)
            if m and cur_t is not None:
                times.append(cur_t); vals.append(float(m.group(1))); cur_t = None
    return times, vals

LZ = sys.argv[1]
DZ = sys.argv[2]
MIN_DUR = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0

t_l, v_l = parse(LZ)
t_d, v_d = parse(DZ)
# The two ROI passes can decode one extra frame at EOF; truncate to the common
# length instead of asserting (a one-frame mismatch failed the whole reframe).
n = min(len(v_l), len(v_d))
t_l, v_l, v_d = t_l[:n], v_l[:n], v_d[:n]
if n == 0:
    # Degenerate clip (no decodable motion frames): no segments, not a crash.
    print("[]")
    print("0 segments, total 0.00s", file=sys.stderr)
    sys.exit(0)

def norm(v):
    m = sum(v) / max(len(v), 1)
    return [x / m if m > 0 else 0 for x in v]

n_l = norm(v_l); n_d = norm(v_d)

WIN = int(sys.argv[4]) if len(sys.argv) > 4 else 15
def smooth(v):
    out = []
    for i in range(len(v)):
        a = max(0, i - WIN // 2); b = min(len(v), i + WIN // 2 + 1)
        out.append(sum(v[a:b]) / (b - a))
    return out

s_l = smooth(n_l); s_d = smooth(n_d)

MARGIN = float(sys.argv[5]) if len(sys.argv) > 5 else 1.15
speaker = []
cur = 0 if s_l[0] >= s_d[0] else 1
for i in range(len(s_l)):
    if cur == 0 and s_d[i] > s_l[i] * MARGIN: cur = 1
    elif cur == 1 and s_l[i] > s_d[i] * MARGIN: cur = 0
    speaker.append(cur)

# Boundaries come from the frames' REAL pts_time (parsed above): frame_index/30
# stretched/compressed the timeline for anything that isn't exactly 30fps.
if len(t_l) > 1:
    deltas = sorted(b - a for a, b in zip(t_l, t_l[1:]))
    frame_dt = max(1e-6, deltas[len(deltas) // 2])  # median inter-frame delta (VFR-safe)
else:
    frame_dt = 1 / 30.0

def t_at(i):
    return t_l[i] if i < len(t_l) else t_l[-1] + frame_dt

segments = []
i = 0
while i < len(speaker):
    j = i
    while j + 1 < len(speaker) and speaker[j + 1] == speaker[i]:
        j += 1
    segments.append({"start": t_at(i), "end": t_at(j + 1),
                     "speaker": "left" if speaker[i] == 0 else "right"})
    i = j + 1

merged = []
for seg in segments:
    if merged and (seg["end"] - seg["start"]) < MIN_DUR:
        merged[-1]["end"] = seg["end"]
    else:
        merged.append(seg)

collapsed = []
for seg in merged:
    if collapsed and collapsed[-1]["speaker"] == seg["speaker"]:
        collapsed[-1]["end"] = seg["end"]
    else:
        collapsed.append(seg)

print(json.dumps(collapsed, indent=2))
total = collapsed[-1]["end"] if collapsed else 0.0
print(f"{len(collapsed)} segments, total {total:.2f}s", file=sys.stderr)
