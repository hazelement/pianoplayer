# AGENTS.md — PianoPlayer

## What This Is

Automatic piano fingering generator. Upload MIDI or MusicXML → get MusicXML with fingering annotations. Combinatorial search algorithm minimizes finger velocity across note sequences.

## Commands

| Action | Command |
|--------|---------|
| Start web app | `python app.py` → http://localhost:5000 |
| Install deps | `pip install -r requirements.txt` |
| Programmatic API | `from pianoplayer.core import annotate_with_args` |
| Production deploy | `gunicorn -w 4 -b 0.0.0.0:5000 app:app` |
| Docker build | `docker build -t pianoplayer .` |

## Architecture

```
app.py                          # Flask entry point (port 5000)
├── pianoplayer/core.py         # Main annotate() logic; entry for all code paths
├── pianoplayer/hand.py         # Hand class — combinatorial fingering algorithm
├── pianoplayer/scorereader.py  # Parses MusicXML (music21) & MIDI (pretty_midi)
├── pianoplayer/utils.py        # keypos(), handSizeFactor(), note name normalization
├── pianoplayer/vkeyboard.py    # 3D visualization (vedo, optional)
└── pianoplayer/wavegenerator.py # Audio generation (optional)

templates/index.html            # Frontend (self-contained, no build step)
scores/                         # Example MusicXML files for testing
```

**Real entrypoint**: `pianoplayer/core.py::annotate(args)` — everything flows through here. `annotate_with_args()` is the programmatic wrapper (used by Flask). `run_annotate()` is the legacy CLI wrapper.

## Key Dependencies

- **music21** — MusicXML parsing & annotation
- **pretty_midi** — MIDI parsing
- **flask + werkzeug** — Web server
- **MuseScore** (system binary) — Required for `.mscz`/`.mscx` conversion; invoked via `os.system()` in `core.py`
- **vedo** — Optional 3D visualization (commented out in requirements.txt)

Python 3.12. Virtual environment at `.venv/`.

## Critical Gotchas

1. **No setup.py or pyproject.toml** — the package is not installed. The Dockerfile uses `ENV PYTHONPATH=/app` to access modules directly from source. Do NOT add a setup.py unless explicitly asked.

2. **Hand size is boolean flags, not a single parameter** — `annotate_with_args()` takes `hand_size_M`, `hand_size_XL`, etc. as separate booleans. Only one should be `True`. The Flask app maps the string hand size to these flags. If you add a new hand size, you must update: `utils.handSizeFactor()`, `core.annotate()` flag handling, `app.py` flag mapping, and `templates/index.html` `<option>`.

3. **MuseScore is a hard system dependency** for `.mscz`/`.mscx` files. The conversion uses `os.system()` (line ~159 in core.py), not a library. If MuseScore is not installed, those formats silently fail.

4. **Files are stored in `tempfile.gettempdir()`** — uploads and outputs go to the system temp directory, not a project-local folder. Input files are deleted after processing; output files are deleted after download via `call_on_close`.

5. **No tests exist** — the `test/` directory contains only `source.mid`. There is no test framework configured.

6. **Algorithm depth is 3–9** — `Hand.optimize_seq()` uses nested loops for up to 9 notes. Depth 0 means "auto" (time-based). The combinatorial search is O(5^depth) in the worst case.

7. **Left hand notes are mirrored** — `Hand.generate()` negates `anote.x` for left hand to simulate mirrored keyboard. This is internal to the algorithm, not a bug.

## File Format Support

| Input | Handler |
|-------|---------|
| `.mid` / `.midi` | `reader_pretty_midi()` via pretty_midi |
| `.xml` / `.musicxml` | `reader()` via music21 |
| `.mscz` / `.mscx` | MuseScore CLI → XML, then `reader()` |

Output is always MusicXML (`.xml`) unless `.txt` is specified (PIG format).

## Hand Size Scale

XXS (0.33x) → XS (0.46x) → S (0.64x) → **M (0.82x, default)** → L (1.0x) → XL (1.1x) → XXL (1.2x)

Factors in `utils.handSizeFactor()` scale the resting finger positions in `Hand.__init__()`.

## PDF Parser Integration

PDF input is supported via dual-engine OMR: built-in vector parser and Audiveris. Routing logic in `conversion.convert_pdf_to_musicxml()`.

| Input | Handler |
|-------|---------|
| `.pdf` (vector) | Built-in parser via `pdf_parser.py` |
| `.pdf` (raster/scanned) | Audiveris (requires `AUDIVERIS_HOME` or `AUDIVERIS_JAR_PATH`) |
| `.pdf` (auto) | Detects type: VECTOR → builtin, RASTER/AMBIGUOUS → Audiveris |

**Engine selection:** `omr_engine` parameter in `annotate_with_args()`: `"auto"` (default), `"builtin"`, `"audiveris"`. Falls back to alternate engine once if primary fails (N-1: no bounce back).

**Multi-page PDFs:** Always routed to Audiveris (N-2). Built-in parser processes first page only.

### PDF Parser Limitations

The built-in vector PDF parser (`pianoplayer/pdf_parser.py`) has these known limitations:

- **Key signature:** Always C major (no sharps/flats detected). Accidentals are inferred from note head position only.
- **Time signature:** Always 4/4. Actual time signatures are not detected.
- **Duration inference:** Uses spatial heuristics (chord spacing, beam detection) only. Accuracy varies with score layout.
- **Multi-page:** First page only. Multi-page PDFs are routed to Audiveris automatically.
- **Accuracy:** ~90-95% on clean vector PDFs. Degrades on complex scores with dense beaming, articulations, or non-standard notation.
- **Articulations/expressions:** Not detected (no dynamics, tempo marks, or articulation symbols).

### PDF Export

Annotated MusicXML → PDF export uses MuseScore CLI (`export_musicxml_to_pdf()` in `conversion.py`). Requires MuseScore 3.x or 4.x installed. In headless environments, set `QT_QPA_PLATFORM=offscreen`.
