"""Tests for the FTS5 (trigram) transcript index — the ADDITIVE accelerator for
/transcripts/search (spec §7.2 "SQLite/FTS5 when search/scale demand it").

The index is only a candidate *filter*: a trigram MATCH returns a superset of the transcripts
whose flat text contains the query substring (trigram → no false negatives for needles ≥3
chars), and the existing word-scan does the exact match + snippet/timing. So the contract under
test is: (a) substring matches — including mid-token — are never missed; (b) anything the index
can't answer (short needle, FTS unavailable, weird query) returns None = "scan everything";
(c) the flat-text builder is shared with the search route so the filter matches what's scanned.
"""
from __future__ import annotations

import json

import transcript_io
from transcript_index import TranscriptIndex, index_words_file


def test_flat_text_shared_builder():
    words = [
        {"idx": 0, "w": "the", "start": 0.0, "end": 0.2},
        {"idx": 1, "w": "gray", "start": 0.2, "end": 0.4, "deleted": True},
        {"idx": 2, "w": "elephant", "start": 0.4, "end": 0.9},
    ]
    flat, char_to_widx = transcript_io.flat_text(words)
    assert flat == "the elephant"          # the deleted word is dropped
    assert len(char_to_widx) == len(flat)  # one word-index per char
    # the 'e' that starts "elephant" maps back to word idx-position 2
    assert char_to_widx[flat.index("elephant")] == 2


def test_index_and_search_candidates_substring_superset(tmp_path):
    ix = TranscriptIndex(str(tmp_path / "ix.sqlite3"))
    assert ix.available, "this SQLite must have the FTS5 trigram tokenizer"
    ix.index("t1", "the elephant in the room")
    ix.index("t2", "an elegant solution")
    ix.index("t3", "Local-First Clip Studio")  # mixed case
    # mid-token substring — the case a word tokenizer would MISS
    assert ix.search_candidates("lepha") == {"t1"}
    assert ix.search_candidates("eleg") == {"t2"}
    # case-insensitive (indexed mixed-case, queried lower)
    assert ix.search_candidates("local") == {"t3"}
    # multi-word phrase with a space
    assert ix.search_candidates("the elephant") == {"t1"}
    # no match → empty set (NOT None — the index *can* answer "nothing")
    assert ix.search_candidates("zzqx") == set()


def test_search_candidates_none_when_it_cannot_help(tmp_path):
    ix = TranscriptIndex(str(tmp_path / "ix.sqlite3"))
    ix.index("t1", "hello world")
    # <3 chars can't form a trigram → None = "scan everything" (don't risk a false negative)
    assert ix.search_candidates("hi") is None
    assert ix.search_candidates("") is None


def test_index_replace_remove_and_persist(tmp_path):
    p = str(tmp_path / "ix.sqlite3")
    ix = TranscriptIndex(p)
    ix.index("t1", "first text with elephant")
    assert ix.indexed_ids() == {"t1"}
    ix.index("t1", "replaced text with giraffe")  # replace, not append
    assert ix.search_candidates("elephant") == set()
    assert ix.search_candidates("giraffe") == {"t1"}
    ix.remove("t1")
    assert ix.indexed_ids() == set()
    # survives a fresh open of the same file
    ix.index("t9", "persisted elephant")
    assert TranscriptIndex(p).search_candidates("elephant") == {"t9"}


def test_fts_special_chars_never_crash(tmp_path):
    ix = TranscriptIndex(str(tmp_path / "ix.sqlite3"))
    ix.index("t1", 'a "quoted" phrase with OR and a star*')
    # FTS query operators in user input must never raise — worst case → None (scan)
    for q in ['"quoted"', "OR", "star*", "a AND b", "(", "near/2"]:
        r = ix.search_candidates(q)
        assert r is None or isinstance(r, set)


def test_index_words_file_indexes_flat_text(tmp_path):
    words_path = tmp_path / "src.words.json"
    doc = {"schema_version": 2, "language": "en", "duration": 1.0, "edited_at": None,
           "words": [{"idx": 0, "w": "rare", "start": 0.0, "end": 0.3},
                     {"idx": 1, "w": "antelope", "start": 0.3, "end": 0.9}],
           "segments": [], "bookmarks": []}
    words_path.write_text(json.dumps(doc))
    ix = TranscriptIndex(str(tmp_path / "ix.sqlite3"))
    index_words_file(ix, "tA", str(words_path))
    assert ix.indexed_ids() == {"tA"}
    assert ix.search_candidates("antel") == {"tA"}  # mid-token substring of "antelope"
    # a missing file is a best-effort no-op (never raises)
    index_words_file(ix, "tMissing", str(tmp_path / "nope.words.json"))
    assert "tMissing" not in ix.indexed_ids()
