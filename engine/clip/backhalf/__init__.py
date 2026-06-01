"""Vendored clip primitives (back-half).

These four modules are adapted, verbatim, from an MIT-licensed upstream and kept
unmodified on purpose — they are the cheap, offline, no-ML core that makes Spool's
reframe work. The Spool wrappers in the parent package call them; do not "improve"
them casually. Full license text and attribution: ``THIRD_PARTY_LICENSES.md``.

- ``roi_motion``   (was analyze.py)    — two ROI motion-energy files → smoothed,
                                          hysteresis speaker timeline (segments JSON).
- ``pan_expr``     (was build_pan.py)  — segments → ffmpeg hard-cut crop-x expression.
- ``ass_captions`` (was build_ass.py)  — whisper words → styled ASS (opus/karaoke/minimal).
- ``audio_xcorr``  (was audio_align.py)— FFT cross-correlation to locate a sub-clip
                                          inside a longer source (de-dupe / find master).
"""
