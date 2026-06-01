"""Spool clip engine — the "back half" of the pipeline.

trove (the modules at the engine root) owns ingest → transcribe → diarize and
the job/store/security/MCP machinery. This package owns everything after a
transcript exists:

    words.json ─▶ find_moments ─▶ cut ─▶ reframe(pan/split/center) ─▶ caption ─▶ export

Modules
-------
- ``moments``   — LLM moment-finding + ranking over ``words.json``.
- ``cutter``    — lossless ``ffmpeg -c copy`` trims.
- ``reframe``   — ROI detection, the diar⊕ROI speaker timeline, and pan/split/center render.
- ``captioner`` — ASS caption generation (words sliced to the clip) + burn-in.
- ``exporter``  — final mux + platform/loudness presets + brand kit.
- ``backhalf``  — vendored ffmpeg/numpy primitives (see THIRD_PARTY_LICENSES.md).

Every public function here is a plain, side-effect-scoped engine function callable
by the Flask API workers and by the MCP server alike — no business logic lives in
the UI or the MCP adapter (spec §3, "Engine package").
"""
