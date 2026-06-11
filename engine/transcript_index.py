"""FTS5 (trigram) transcript index — an ADDITIVE accelerator for ``/transcripts/search``.

Spec §7.2 records the call: "JSON now; SQLite (FTS5) when search/scale demand it." This is the
*additive, no-user-facing-change* form of that — NOT the whole-job-store migration (deliberately
left alone; it's high-risk and optimization-only). The in-memory word-scan in ``routes/api_v1``
stays the source of truth: it always reads the current ``words.json`` (so it reflects edits and
deletes instantly) and extracts the snippet + start/end timing for deep-linking.

What this adds: a trigram FTS5 table mapping ``transcript_id → lowercased flat text``. A trigram
``MATCH`` returns a *superset* of the transcripts whose text contains a query substring — trigram
indexes 3-char shingles, so a true substring (≥3 chars) always shares every trigram with some
document and is never missed (false negatives = 0); the false positives are filtered out by the
exact word-scan. The search route asks for candidate ids, then scans only those.

Correctness never depends on the index — every uncertain case degrades to today's full scan:
  * ``search_candidates`` returns ``None`` ("scan everything") when the needle is <3 chars, the
    FTS5 trigram tokenizer isn't available, or a query errors.
  * the caller never skips a transcript that isn't in the index (so an empty / lagging / lost
    index just means "no acceleration", never a missed hit).
  * the index is kept fresh at the two write points the search depends on — transcript-done and
    the word-edit endpoint — and the search lazily backfills any unindexed transcript it scans.
"""
from __future__ import annotations

import sqlite3
import threading

import transcript_io


def _trigram_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE IF EXISTS _probe")
        return True
    except sqlite3.Error:
        return False


def _as_phrase(needle: str) -> str:
    """Quote the needle as a single FTS5 phrase so query operators in user input (", *, OR,
    NEAR, parens) are matched literally rather than parsed."""
    return '"' + needle.replace('"', '""') + '"'


class TranscriptIndex:
    """A tiny SQLite FTS5 (trigram) store: transcript_id → lowercased flat text.

    Thread-safe under Flask's threaded server: every method uses a short-lived connection
    created and closed within the call (so it never crosses threads), and writes are serialized
    by a lock. If FTS5/trigram isn't available the store is inert (``available == False``) and
    every query returns ``None`` so the caller full-scans.
    """

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self.available = False
        try:
            with self._connect() as c:
                if _trigram_available(c):
                    c.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS transcripts "
                        "USING fts5(tid UNINDEXED, body, tokenize='trigram')"
                    )
                    self.available = True
        except sqlite3.Error:
            self.available = False

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def index(self, tid: str, text: str) -> None:
        """(Re)index a transcript's flat text — replaces any prior row for ``tid``."""
        if not self.available:
            return
        try:
            with self._lock, self._connect() as c:
                c.execute("DELETE FROM transcripts WHERE tid = ?", (tid,))
                c.execute("INSERT INTO transcripts(tid, body) VALUES (?, ?)", (tid, (text or "").lower()))
        except sqlite3.Error:
            pass

    def remove(self, tid: str) -> None:
        if not self.available:
            return
        try:
            with self._lock, self._connect() as c:
                c.execute("DELETE FROM transcripts WHERE tid = ?", (tid,))
        except sqlite3.Error:
            pass

    def indexed_ids(self) -> set:
        if not self.available:
            return set()
        try:
            with self._lock, self._connect() as c:
                return {r[0] for r in c.execute("SELECT tid FROM transcripts")}
        except sqlite3.Error:
            return set()

    def search_candidates(self, needle: str):
        """Transcript ids whose text *may* contain ``needle`` (a superset), or ``None`` meaning
        "can't help — scan everything" (needle <3 chars, store unavailable, or a query error)."""
        n = (needle or "").lower()
        if not self.available or len(n) < 3:
            return None
        try:
            with self._lock, self._connect() as c:
                rows = c.execute(
                    "SELECT tid FROM transcripts WHERE transcripts MATCH ?", (_as_phrase(n),)
                )
                return {r[0] for r in rows}
        except sqlite3.Error:
            return None


def index_words_file(index: "TranscriptIndex", tid: str, words_path: str) -> None:
    """Best-effort: load a ``words.json`` and (re)index its flat text. Used at transcript-done
    and on a word edit. Any failure (missing/corrupt file, FTS error) is swallowed — the index
    is an optimization, never a correctness dependency."""
    if index is None or not getattr(index, "available", False):
        return
    try:
        data = transcript_io.load(words_path)
    except (OSError, ValueError):
        return
    flat, _ = transcript_io.flat_text(data.get("words") or [], data.get("segments") or [])
    index.index(tid, flat)
