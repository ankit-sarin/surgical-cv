Read primer.md for current project state before starting work.

# Surgical Computer Vision (Project 5)

## Location
~/projects/surgical-cv

## Deployment
No deployed services yet. Future services will follow global conventions (systemd + Cloudflare Tunnel).

## Purpose
End-to-end surgical video research: FFmpeg de-identification pipeline (raw → deid), dataset preprocessing, annotation tooling, phase recognition, instrument detection, model training/evaluation, and future deployed services. Targets ~3 publications: (1) dataset/benchmark paper, (2) phase recognition model, (3) clinical application (AI-assisted video editing).

## PI & Context
- **PI:** Ankit Sarin, MD — Chief, Colorectal Surgery, UC Davis Health
- **Lab:** PLUM Lab collaboration
- **IRB:** Approved; de-identification SOP in place
- **Hardware:** NVIDIA DGX Spark (Blackwell GB10, 128GB unified memory)

## Data Sources (STRIVE NAS via NFS)

### Raw OR Video (PHI — read-write)
| Path | Contents |
|------|----------|
| /mnt/nas/raw-sarin/ | Raw video — Sarin cases |
| /mnt/nas/raw-miller/ | Raw video — Miller cases |
| /mnt/nas/raw-noren/ | Raw video — Noren cases |
| /mnt/nas/raw-flynn/ | Raw video — Flynn cases |
| /mnt/nas/raw-kucejko/ | Raw video — Kucejko cases |

### De-identified Output (read-write)
| Path | Contents |
|------|----------|
| /mnt/nas/deid-sarin/ | De-identified video — Sarin cases |
| /mnt/nas/deid-miller/ | De-identified video — Miller cases |
| /mnt/nas/deid-noren/ | De-identified video — Noren cases |
| /mnt/nas/deid-flynn/ | De-identified video — Flynn cases |
| /mnt/nas/deid-kucejko/ | De-identified video — Kucejko cases |

### Collaboration Shares (read-write)
| Path | Contents |
|------|----------|
| /mnt/nas/deid-shared/ | De-identified video — shared pool |
| /mnt/nas/deid-plum/ | De-identified video — PLUM Lab share |

### Public Datasets
| Path | Contents |
|------|----------|
| /mnt/nas/research-datasets/ | Cholec80, CholecT50, etc. |

Raw video is never copied into this repo. NAS paths are referenced, not duplicated.

## OR Video Pipeline (Three-Pass BDV Workflow)

Video arrives from the STERIS/BDV recording system as multiple ~2GB segments
per case (naming convention: `capt0_YYYYMMDD-HHMMSS.mp4`). Multi-segment input
is the standard, not an edge case. Processing is orchestrated by the in-repo
`pipeline` CLI. State for every case is tracked in two CSVs under
`/mnt/nas/or-raw/` plus a JSONL audit log.

Stage machine (in `pipeline/schemas.py`):
`intake → concatenated → deidentified → verified`, with `failed` reachable
from any non-terminal stage and a single `failed → deidentified` retry edge.

### Study Code Convention
Format: `UCD-FIL-###` (assigned in REDCap). The MRN ↔ study code mapping exists
only in REDCap. Study codes never appear in code, configs, or logs alongside
identifiable information.

### Pass 1: Concat segments → PHI master
- **Input:** BDV segments in `/mnt/nas/raw-[surgeon]/`, listed in `pipeline_state.csv` at `stage=intake`.
- **Tool:** `python -m pipeline concat --surgeon <name>` (batch over all intake rows for that surgeon).
- **Output:** `/mnt/nas/or-raw/<surgeon>_<YYYYMMDD-HHMMSS>.mp4`, where the timestamp is the first segment's BDV timestamp. Atomic via `<name>.partial.mp4` rename.
- **Verify:** Output duration ≈ sum of segment durations; size ≈ sum of source segment sizes (stream-copy concat).
- **Cleanup:** Source segments renamed to `capt0_YYYYMMDD-HHMMSS-copied.mp4` (best-effort; case is already committed before this step).

### Pass 2: De-identify PHI master → research copy
- **Input:** `/mnt/nas/or-raw/<surgeon>_<YYYYMMDD-HHMMSS>.mp4`, listed in `pipeline_state.csv` at `stage=concatenated` with the filename in `concat_filename`.
- **Tool:** `python -m pipeline deid --surgeon <name> [--case UCD-FIL-###]`. Batch mode advances every `concatenated` row for that surgeon; `--case` advances exactly one.
- **Output:** `/mnt/nas/deid-[surgeon]/UCD-FIL-###_video.mp4`. Opaque filename — no date or surgeon name leaks. Atomic via `.partial.mp4` rename.
- **FFmpeg flags:** `-an -map_metadata -1 -c:v libx264 -crf 18 -movflags +faststart`. Strips audio, clears container-level metadata, re-encodes video at visually-lossless quality (CRF 18 typically produces 1.15–1.50× the source size on motion-rich surgical content — expected, not a bug).
- **Verify:** handed off to Pass 3 — independent two-layer check (deterministic preflight + LLM diagnostician), not run inline.
- **Cleanup:** PHI master retention policy TBD.

### Pass 3: Verify de-identification
- **Input:** `/mnt/nas/deid-[surgeon]/UCD-FIL-###_video.mp4`, listed in `pipeline_state.csv` at `stage=deidentified` (or `stage=failed` for retry).
- **Tool:** `python -m pipeline verify --surgeon <name> [--case UCD-FIL-###]`. Batch mode advances every eligible row for that surgeon; `--case` advances exactly one.
- **Two-layer check** (both run against the same evidence dict — ffprobe `-show_format -show_streams`, exiftool `-j`, and `ffmpeg -f null -` null-mux stderr collected at verify time, NOT captured from the original deid encode):
  1. **Deterministic preflight (PF1/PF2/PF3):** PF1 zero audio streams; PF2 no forbidden metadata keys (`title`/`comment`/`artist`/`composer`/`creator`/`description`/`genre`/`location`/`gps*`/etc.) and encoder/handler allow-list `^(Lavf|Lavc|libx264|VideoHandler|SoundHandler|GPAC).*$`; PF3 filename matches `^UCD-FIL-\d{3}_video\.mp4$`. Short-circuits on first failure.
  2. **LLM diagnostician:** evidence fed to qwen3:32b via Ollama with `format="json"` and `think=False`. Response validated against the frozen `DiagnosticianVerdict` schema (`verdict ∈ {pass, fail}`, `reason` 1–200 chars, `evidence[]` up to 10 strings). One retry on parse/schema failure; no retry on daemon-level failures (those abort the batch).
- **Output:** state transition `deidentified|failed → verified|failed` with `verify_ts` populated. No new files written.
- **Exit codes (three-tier):** 0 all verified or none eligible, 1 ≥1 clean fail verdict, 2 ≥1 infra error (ollama unavailable aborts immediately; malformed output continues and exits 2 at end).
- **Audit:** every per-case decision writes one entry. Failure kinds: `preflight`, `diagnostician`, `infra`, `exception`.

### File Naming Convention
| Pattern | Usage |
|---------|-------|
| `<surgeon>_<YYYYMMDD-HHMMSS>.mp4` | PHI master (concat output) in `/mnt/nas/or-raw/`. Timestamp is the first segment's BDV timestamp. |
| `UCD-FIL-###_video.mp4` | De-identified full video in `deid-[surgeon]/`. Opaque — no date/surgeon leak. |
| `UCD-FIL-###_phase-NN_description.mp4` | Phase clip from de-identified video |
| `UCD-FIL-###_edited-full.mp4` | Concatenated teaching edit |
| `UCD-FIL-###_opnote.txt` | De-identified operative note |
| `UCD-FIL-###_meta.json` | Case metadata (procedure type, approach, duration) |

### Folder Routing
The operating surgeon determines both the source and destination folders.
When the user says "de-identify Miller's latest case," that means:
- Read segments from `/mnt/nas/raw-miller/`
- Write PHI master to `/mnt/nas/raw-miller/`
- Write de-identified output to `/mnt/nas/deid-miller/`

### Curation (post-deid)
PI curates subsets from per-surgeon deid folders into collaboration shares:
- `/mnt/nas/deid-shared/` — internal shared pool
- `/mnt/nas/deid-plum/` — PLUM Lab collaboration
- `/mnt/nas/deid-anaut/` — Anaut collaboration
This is a manual copy step, not part of the automated pipeline.

## Manifest Metadata

Inspect or correct rows in `case_manifest.csv` via:

    python -m pipeline metadata UCD-FIL-### [--show | --edit FIELD VALUE [--confirm]]

Three modes:

- **`--show` (default):** renders all 8 manifest fields plus a one-line pipeline-stage summary. Read-only; silent on the audit log.
- **`--edit FIELD VALUE`:** validates the proposed value against field-specific rules (regex for `case_year`, non-empty `.strip()` for `or_room`, picklist membership for `procedure_name`/`approach`/`indication`, anything for `notes`). On success, renders a "DRY RUN" preview with `Before` / `After`. No state change; silent on the audit log.
- **`--edit FIELD VALUE --confirm`:** atomic commit via `CsvTable.transaction()`. The `before` value is captured from the locked snapshot inside the transaction (not from the pre-snapshot existence check). Writes one audit entry per invocation, success or failure.

Six editable fields: `case_year`, `or_room`, `procedure_name`, `approach`, `indication`, `notes`.
Two immutable fields (never editable via this CLI): `ucd_fil_id`, `surgeon`.

Notes-field commits emit a soft PHI nudge to stderr:
`note: free-text field; PHI screening happens downstream via surgeon-audit.`

### Vocabularies

Picklist values for `procedure_name` / `approach` / `indication` are loaded from `bench/vocabularies/*.json` at validation time. Override the directory with `PIPELINE_VOCAB_DIR` (used by hermetic tests).

| Vocabulary | Items | Notes |
|------|------|------|
| procedures.json | 19 | Semantic clinical ordering, "Other" last. Includes TAMIS in the rectal-cancer cluster (LAR / APR / TAMIS). |
| approaches.json | 4 | Open / Laparoscopic / Robotic / Hybrid |
| indications.json | 11 | "Other" last |
| case_years.json | — | Not provided. `case_year` is validated by regex `^\d{4}$` only (no allowlist). |

## Audit Log

Every state-changing subcommand writes one JSONL entry per invocation to `/mnt/nas/or-raw/pipeline.log` via `log_audit()` in `pipeline/audit.py`. Concurrent writers are serialized by `fcntl.LOCK_EX` on a sibling `pipeline.log.lock` file.

Required keys per entry: `ts` (ISO 8601 UTC), `pid`, `operator` (`$USER` or `"unknown"`), `command`, `args` (dict), `outcome` ∈ `{"success", "failure"}`.
Optional keys: `case` (omitted when not applicable, e.g. format failures where the case ID itself is malformed), `details` (omitted when None).

Granular failure categories are recorded in `details.failure_kind` rather than expanding the `outcome` literal. Current taxonomy:

| Subcommand | `failure_kind` values | Exit on each |
|------------|-----------------------|--------------|
| verify     | preflight, diagnostician, infra, exception | 1 / 1 / 2 / 2 |
| metadata   | format, not_found, validation, infra, exception | 1 / 1 / 1 / 2 / 2 |
| concat, deid | (no discriminator yet — error info goes directly into `details.error` / `details.error_type`) | 1 |

Read paths (`status`, `metadata --show`) and dry-runs (`metadata --edit` without `--confirm`) do not write to the audit log.

## State Files & Atomicity

Two CSVs live under `/mnt/nas/or-raw/`:
- `case_manifest.csv` — per-case manifest (8 columns; rows mutated only via `metadata --edit --confirm`)
- `pipeline_state.csv` — per-case stage tracking (`raw_segments`, `concat_filename`, `deid_filename`, `stage`, `concat_ts`, `deid_ts`, `verify_ts`, `verification_notes`)

All mutations go through `CsvTable.transaction()` in `pipeline/csv_io.py`:
1. Acquire `fcntl.LOCK_EX` on a sibling `.lock` file
2. Read rows under the lock
3. Yield a `Transaction` object that buffers `append` / `update` calls
4. On context-manager exit (no exception): re-serialize rows to a tempfile via `csv.DictWriter`, `fsync`, then `os.replace` for atomic rename
5. On exception inside the `with`: skip the commit step entirely — the original file is untouched

This is the only sanctioned write path. Direct `open(csv, "w")` is forbidden.

## Project Structure
```
surgical-cv/
├── CLAUDE.md              # Static architecture (this file)
├── primer.md              # Working state (maintained by Claude Code)
├── README.md              # Pipeline CLI quick reference
├── requirements.txt
├── pipeline/              # CLI: concat, deid, verify, status, metadata
│   ├── __main__.py
│   ├── cli.py             # argparse + dispatch (incl. metadata --edit FIELD validation Action)
│   ├── schemas.py         # Pydantic v2 row models + stage machine + DiagnosticianVerdict
│   ├── csv_io.py          # locked atomic CSV I/O (CsvTable.transaction)
│   ├── audit.py           # JSONL audit logger (log_audit)
│   ├── paths.py           # NAS path resolution
│   ├── ffmpeg.py          # ffmpeg_concat, ffmpeg_deid, ffprobe helpers
│   ├── diagnostician.py   # qwen3:32b Ollama harness for verify (collect_evidence, diagnose)
│   └── commands/          # one module per subcommand (concat, deid, verify, status, metadata)
├── tests/                 # 263 tests covering all of the above
├── bench/                 # model benchmark harness (pre-existing)
├── scripts/               # Throwaway scripts (e.g. seed_intake.py — gitignored)
├── configs/               # Experiment configs (YAML) — placeholder
├── notebooks/             # Exploratory analysis — placeholder
└── data/                  # gitignored — local caches, extracted frames
```

## Scope
- FFmpeg de-identification pipeline (raw → deid)
- Video frame extraction and preprocessing
- Dataset curation tooling (annotation, splits, statistics)
- Surgical phase recognition models
- Instrument detection models
- Benchmark evaluation against public datasets
- Publication figures and tables
- Future deployed services (Gradio, APIs) as needed

## Inference
- **Primary:** Ollama at localhost:11434 for local LLM tasks (annotation assistance, etc.)
- **Vision models:** MiniCPM-V, Qwen2.5-VL-7B, Qwen3-VL:8b (confirmed working on DGX)
- **Verify diagnostician (current):** qwen3:32b via Ollama with `format="json"`, `think=False`, `temperature=0`. Output validated against `DiagnosticianVerdict` (frozen Pydantic v2 schema in `pipeline/schemas.py`). All calls go through `pipeline/diagnostician.py::diagnose()` — the single chokepoint for future TensorRT-LLM swap.
- **PyTorch:** CPU-only or ONNX Runtime until Blackwell sm_121 gets PyTorch CUDA support
- Wrap all `ollama.chat()` calls in isolated methods for future TensorRT-LLM swap

## Key Patterns
- Experiment configs in YAML — one file per experiment, reproducible
- All video paths are NAS references, never local copies
- Frame extraction is a preprocessing step, cached in data/ (gitignored)
- Public dataset loaders should handle Cholec80/CholecT50 directory conventions
- Cross-family model verification for any LLM-assisted annotation
- De-identification pipeline: concat from raw-[surgeon] to or-raw, then de-identify to deid-[surgeon]. Orchestrated by the in-repo `pipeline` CLI; state in `or-raw/{case_manifest,pipeline_state}.csv` + `pipeline.log`.

## Running
```bash
# Activate venv
source venv/bin/activate

# Tests
python -m pytest tests/ -v
python -m pytest tests/ -v -m "not slow"    # skip long-running tests

# De-identification pipeline
python -m pipeline status                            # joined view of all cases
python -m pipeline concat --surgeon sarin            # batch concat intake → concatenated
python -m pipeline deid   --surgeon sarin            # batch deid concatenated → deidentified
python -m pipeline deid   --surgeon sarin --case UCD-FIL-001  # single-case deid
python -m pipeline verify --surgeon sarin            # batch verify deidentified|failed → verified|failed
python -m pipeline verify --surgeon sarin --case UCD-FIL-001  # single-case verify

# Manifest metadata
python -m pipeline metadata UCD-FIL-001                                          # show all 8 fields + stage
python -m pipeline metadata UCD-FIL-001 --edit procedure_name "Sigmoidectomy"    # dry-run preview
python -m pipeline metadata UCD-FIL-001 --edit procedure_name "Sigmoidectomy" --confirm  # commit

# Frame extraction (example)
python scripts/extract_frames.py --config configs/cholec80_frames.yaml

# Training (example)
python scripts/train.py --config configs/phase_recognition_v1.yaml

# Evaluation (example)
python scripts/evaluate.py --config configs/eval_cholec80.yaml
```
