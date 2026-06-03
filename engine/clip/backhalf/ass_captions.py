# Spool engine · clip back-half primitive (vendored, adapted from an MIT-licensed
# upstream). The opus/karaoke/minimal algorithm is preserved; Spool added additive,
# off-by-default caption-craft options (speaker color, balanced line-breaking, keyword
# emphasis) that reproduce the original output byte-for-byte when unset. Full license +
# attribution live in THIRD_PARTY_LICENSES.md at the repo root.
#!/usr/bin/env python3
"""Generate opus-clips style ASS subtitles from whisper word timestamps.

Usage: build_ass.py WHISPER.json OUT.ass [STYLE] [OVERRIDES_JSON]
STYLE: 'opus' (default), 'karaoke', 'minimal'
OVERRIDES_JSON: optional preset overrides, incl. Spool caption-craft:
  speaker_colors {label: "&H00BBGGRR&"}  · balance true  · emphasis "auto"|[words]
"""
import json
import math
import sys

PLAY_W, PLAY_H = 1080, 1920

PRESETS = {
    "opus":     dict(font="Arial Black", size=100, chunk=3, highlight="&H0000FFFF&", outline=8, shadow=3),
    "karaoke":  dict(font="Arial Black", size=110, chunk=4, highlight="&H0000FF00&", outline=6, shadow=2),
    "minimal":  dict(font="Helvetica",   size=70,  chunk=6, highlight=None,          outline=4, shadow=1),
}
WHITE = "&H00FFFFFF&"
OUTLINE = "&H00000000&"


def fmt_time(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t - h*3600 - m*60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_chunks(words, size, balance=False):
    """Group words into caption chunks. Default: fixed ``size``-word slices (byte-identical to
    the original). ``balance=True`` rebalances so chunk sizes differ by at most one — which also
    removes a 1-word orphan trailing chunk (e.g. 7 words @3 → [3,2,2], not [3,3,1])."""
    n = len(words)
    if not balance or n <= size:
        return [words[i:i+size] for i in range(0, n, size)]
    k = math.ceil(n / size)                 # keep the same number of chunks as the fixed slice
    base, rem = divmod(n, k)
    sizes = [base + 1] * rem + [base] * (k - rem)
    out, idx = [], 0
    for s in sizes:
        out.append(words[idx:idx+s]); idx += s
    return out


def _is_keyword(raw, emphasis, allcaps):
    """Whether ``raw`` (source word text) should be emphasized. ``emphasis`` is a list of
    keywords (case-insensitive, punctuation-stripped) or "auto" (a source word in ALL-CAPS,
    e.g. an acronym/shouted word — skipped when the whole caption is already all-caps)."""
    if not emphasis:
        return False
    key = "".join(ch for ch in raw if ch.isalnum()).lower()
    if isinstance(emphasis, (list, tuple, set)):
        return bool(key) and key in {str(k).lower() for k in emphasis}
    alpha = [ch for ch in raw if ch.isalpha()]          # "auto"
    return len(alpha) >= 2 and raw == raw.upper() and not allcaps


def _token(ww, active, *, primary, highlight, speaker_colors, emphasis, allcaps):
    """One word's ASS token: speaker color (base), active-word highlight, keyword scale-up.

    With no speaker_colors and no emphasis this returns exactly the original markup — a plain
    word, or ``{\\c<highlight>}word{\\c<primary>}`` for the active word — so default output is
    byte-identical."""
    text = ww["text"].upper() if allcaps else ww["text"]
    if _is_keyword(ww["text"], emphasis, allcaps):
        text = f"{{\\fscx120\\fscy120}}{text}{{\\fscx100\\fscy100}}"
    base = speaker_colors.get(ww.get("speaker"), primary) if speaker_colors else primary
    if active and highlight:
        return f"{{\\c{highlight}}}{text}{{\\c{base}}}"
    if base != primary:
        return f"{{\\c{base}}}{text}{{\\c{primary}}}"
    return text


def build_events(chunks, *, primary, highlight, speaker_colors, emphasis, allcaps):
    events = []
    for chunk in chunks:
        chunk_end = chunk[-1]["end"]
        for i, w in enumerate(chunk):
            seg_start = w["start"]
            seg_end = chunk[i+1]["start"] if i+1 < len(chunk) else chunk_end
            if seg_end <= seg_start:
                seg_end = seg_start + 0.05
            line = " ".join(
                _token(ww, (j == i), primary=primary, highlight=highlight,
                       speaker_colors=speaker_colors, emphasis=emphasis, allcaps=allcaps)
                for j, ww in enumerate(chunk))
            events.append(f"Dialogue: 0,{fmt_time(seg_start)},{fmt_time(seg_end)},Default,,0,0,0,,{line}")
    return events


def render(words, style="opus", overrides=None):
    """Build the full ASS document string from word dicts ``{start, end, text, speaker?}``."""
    P = dict(PRESETS.get(style, PRESETS["opus"]))
    P.update(overrides or {})
    primary = P.get("primary", WHITE)
    highlight = P["highlight"]
    speaker_colors = P.get("speaker_colors") or {}
    emphasis = P.get("emphasis")
    allcaps = bool(P.get("allcaps"))

    chunks = build_chunks(words, P["chunk"], balance=bool(P.get("balance")))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_W}
PlayResY: {PLAY_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{P["font"]},{P["size"]},{primary},&H000000FF,{OUTLINE},&H00000000,{P.get("bold", 1)},0,0,0,100,100,0,0,1,{P["outline"]},{P["shadow"]},2,60,60,{P.get("marginv", 280)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = build_events(chunks, primary=primary, highlight=highlight,
                          speaker_colors=speaker_colors, emphasis=emphasis, allcaps=allcaps)
    return header, events


def main(argv):
    whisper_json = argv[1]
    out_ass = argv[2]
    style = argv[3] if len(argv) > 3 else "opus"
    overrides = json.loads(argv[4]) if len(argv) > 4 else {}

    data = json.load(open(whisper_json))
    words = []
    for seg in data["segments"]:
        for w in seg.get("words", []):
            words.append({"start": w["start"], "end": w["end"],
                          "text": w["word"].strip(), "speaker": w.get("speaker")})

    header, events = render(words, style, overrides)
    with open(out_ass, "w") as f:
        f.write(header + "\n".join(events) + "\n")
    print(f"wrote {out_ass}: {len(events)} events", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv)
