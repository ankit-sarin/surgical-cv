Read primer.md for current project state before starting work.

# Surgical Computer Vision (Project 5)

## Location
~/projects/surgical-cv

## Deployment
- **`surgical-cv-app.service`** — FastAPI + Gradio surgeon/admin app at `https://cv.digitalsurgeon.dev` via Cloudflare Tunnel, port 7865.
  - Surgeon UI mounted at `/app` (DSM-authenticated, `role='surgeon'`).
  - Admin UI mounted at `/admin` (DSM-authenticated, `role='admin'`).
  - `/login`, `/logout`, `/login/otp` are role-agnostic FastAPI routes.
  - Restart: `sudo systemctl restart surgical-cv-app.service`. WorkingDirectory is the repo; `.env` carries `APP_SESSION_SECRET` and `NAS_DSM_URL`. `APP_DB_PATH` defaults to `app/db/app.db`.
- **Worker (`surgical-cv-worker.timer` / `.service`)** — systemd templates in `deploy/systemd/`, not installed automatically. Per `deploy/README.md`: user-scoped install with `OnUnitActiveSec=5min`, `AccuracySec=30s`, `Persistent=true`.

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

- **`--show` (default):** renders all 10 manifest fields (Spec J schema) plus a one-line pipeline-stage summary. Read-only; silent on the audit log. **F-004:** the `Notes:` row renders as `<redacted, length=N>` for non-empty values (or `(empty)`); the actual notes content never appears on stdout. Operators who need notes content read `case_manifest.csv` directly (file access, not terminal output, doesn't hit journalctl).
- **`--edit FIELD VALUE`:** validates the proposed value against field-specific rules (regex for `case_year`, non-empty `.strip()` for `or_room`, picklist membership for `procedure_primary` / `procedure_additional` / `approach` / `conversion_target` / `indication`). On success, renders a "DRY RUN" preview with `Before` / `After`. No state change; silent on the audit log.
- **`--edit FIELD VALUE --confirm`:** atomic commit via `CsvTable.transaction()`. The `before` value is captured from the locked snapshot inside the transaction (not from the pre-snapshot existence check). Writes one audit entry per invocation, success or failure.

Editable fields (F-003): `case_year`, `or_room`, `procedure_primary`, `procedure_additional`, `approach`, `conversion_target`, `indication`. **`notes` is operator-blocked at the CLI** — `--edit notes` (with or without `--confirm`) refuses with a policy message, no audit entry, no manifest read. Free-text notes editing belongs in the surgeon UI when it's built.
Two immutable fields (never editable via this CLI): `ucd_fil_id`, `surgeon`.

### Vocabularies

Picklist values for `procedure_primary` / `procedure_additional` / `approach` / `conversion_target` / `indication` / `case_year` are loaded from `app/db/seeds/picklists/*.json` at validation time. Override the directory with `PIPELINE_PICKLIST_DIR` (used by hermetic tests).

| Picklist seed | Specialty | Items | Notes |
|------|------|------|------|
| `procedure_colorectal.json` | colorectal | 24 | Semantic clinical ordering, "Other" pinned at sort_order 999. Includes TAMIS in the rectal-cancer cluster (LAR / APR / TAMIS). |
| `approach.json` | universal | 4 | Open / Laparoscopic / Robotic / Hybrid |
| `indication_colorectal.json` | colorectal | 11 | "Other" last |
| `case_year.json` | universal | 16 | "2030"→"2015" descending (newest first — UX win for the intake dropdown). `case_year` is validated by regex `^\d{4}$` first, then allowlist membership. |

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
- `case_manifest.csv` — per-case manifest (10 columns post Spec J: `ucd_fil_id`, `surgeon`, `case_year`, `or_room`, `procedure_primary`, `procedure_additional` (JSON-encoded list), `approach`, `conversion_target`, `indication`, `notes`). Rows mutated only via `metadata --edit --confirm` or the surgeon intake submit path.
- `pipeline_state.csv` — per-case stage tracking (`ucd_fil_id`, `raw_segments`, `concat_filename`, `deid_filename`, `stage`, `intake_ts`, `concat_ts`, `deid_ts`, `verify_ts`, `verification_notes`)

All mutations go through `CsvTable.transaction()` in `pipeline/csv_io.py`:
1. Acquire `fcntl.LOCK_EX` on a sibling `.lock` file
2. Read rows under the lock
3. Yield a `Transaction` object that buffers `append` / `update` calls
4. On context-manager exit (no exception): re-serialize rows to a tempfile via `csv.DictWriter`, `fsync`, then `os.replace` for atomic rename
5. On exception inside the `with`: skip the commit step entirely — the original file is untouched

This is the only sanctioned write path. Direct `open(csv, "w")` is forbidden.

## App Database & Admin Audit (Brief #4)

SQLite at `app/db/app.db`. Six tables: `specialties`, `users`, `picklist_values`,
`attention_items`, `admin_audit`, `scope_violation_log`. Brief #4 introduced two
schema changes:

- `attention_items.updated_at TEXT NOT NULL` (Brief #3.5b in `schema.sql`,
  migrated live in #4a) — advances on every `upsert_by_case_and_type` call;
  equals `created_at` for plain inserts. The companion partial unique index
  `idx_attention_phi_redacted_case_uniq` enforces "exactly one open
  `phi_redacted` row per case_id" — scope intentionally narrowed to that
  type only so existing retry semantics on `verify_soft_fail` /
  `pipeline_failure` / `orphan_marker` rows (today plain INSERTs) keep
  working.
- `admin_audit` renamed `admin_username` → `actor_username`, added
  `actor_role TEXT NOT NULL CHECK (actor_role IN ('surgeon', 'admin'))`, and
  added `resolved_on_behalf_of TEXT` nullable. Application code MUST specify
  `actor_role` on every insert (no DEFAULT in steady-state schema.sql; the
  migration's ALTER carries a `DEFAULT 'admin'` for backfill only).

Migration runner: `python -m app.db.migrate_brief_4 --dry-run | --commit`.
Writes `app.db.pre-brief-4.<utc-ts>.bak` before any DDL fires under
`--commit`. No general migration tooling yet — that's Brief #5. Existing
`scripts/migrate_manifest_spec_j.py` is the CSV-only one-shot precedent;
SQLite migrations live under `app/db/` because `scripts/` is gitignored.

## Admin App (Brief #4)

Gradio Blocks at `app/admin_app.py`, mounted at `/admin` with
`_gradio_auth_dep("admin")`. Two tabs:

- **Global Dashboard** — single `gr.HTML` stat strip (5 tiles via CSS grid,
  no flex-wrap — Brief #3.1 cycle avoidance) + per-surgeon `gr.DataFrame`.
  Stats: total cases, in intake, open AR items, high-severity AR, stale
  (no activity >7d). Per-surgeon table groups by `users.folder_slug`,
  alphabetical by username.
- **Action Required** — cross-silo `gr.DataFrame` (one row per open AR item,
  surgeon-as-folder_slug visible) with four filters
  (type / surgeon / severity / older-than-N-days), all AND-together.
  Row select populates a detail panel; admin dismisses with a free-text
  reason (server-side gated at `ADMIN_REASON_MIN_LENGTH = 10` chars) or
  resolves on behalf of a surgeon (the surgeon's username lands in
  `admin_audit.resolved_on_behalf_of`). `admin_resolve` / `admin_dismiss`
  on the repo bypass the surgeon-side type/scope validation — admin is the
  override path.

CSS lessons enforced: text-affecting rules only (`font-family`, `color`,
`font-size`), no flex-wrap / margin: auto / layout transitions on
admin DOM. `tab.select` event-source NOT paired with `blocks.load` on
the same render fn — multi-source-into-same-output was the wiring shape
that re-tickled the cycle on first deploy.

## Repositories (`app/repos/`)

| Repo | Methods | Used by |
|---|---|---|
| `CaseRepository` (Csv / InMemory) | `list_owned_by`, `get_case`, `case_belongs_to`, `submit_case`, **`list_all`** (Brief #4) | surgeon scope + admin Global Dashboard |
| `RawSegmentRepository` (Filesystem / InMemory) | `list_raw_segments` | intake Section 1 |
| `PicklistRepository` (Sqlite / InMemory) | `list_active` | intake dropdowns, metadata validation |
| `PipelineStateRepository` (Csv / InMemory) | `list_for_case_ids`, `get_state`, **`list_all`** + **`case_id_for_source_file`** (Brief #4) | My Cases + admin Global Dashboard |
| `AttentionItemsRepository` (Sqlite / InMemory) | `has_attention_for_case_ids`, `list_for_user`, **`list_all`** (Brief #4), `resolve`, `dismiss`, **`admin_resolve`** + **`admin_dismiss`** (Brief #4), `upsert_by_case_and_type` (Brief #3.5b), `count_actions_today` | Action Required tab + cross-silo admin AR |
| `CaseManifestRepository` (Csv / InMemory) | `for_case_id` | typed manifest reads |

`list_all` is unscoped by design — no role check inside the repo. Auth
boundary lives at the `/admin` mount's role guard.
`case_id_for_source_file` raises `MultipleClaimsError` (in
`app/exceptions.py`) if a segment is claimed by more than one case —
surfaces pipeline state corruption rather than silently picking one.

## Project Structure
```
surgical-cv/
├── CLAUDE.md              # Static architecture (this file)
├── primer.md              # Working state (gitignored; maintained by Claude Code)
├── README.md              # Pipeline CLI quick reference
├── requirements.txt       # Includes cryptography>=42 (added for F-008 Fernet wrap)
├── docs/
│   ├── audits/            # Codebase audits (immutable snapshots, not edited post-write)
│   └── audit_deferrals.md # Findings from audits explicitly accepted vs. fixed
├── pipeline/              # CLI: concat, deid, verify, status, metadata
│   ├── __main__.py
│   ├── cli.py             # argparse + dispatch
│   ├── schemas.py         # Pydantic v2 row models + stage machine + DiagnosticianVerdict
│   │                      # Plus shared regexes (CASE_ID_RE, SURGEON_RE) and
│   │                      # constants (VERIFICATION_NOTES_MAX) per F-016/F-017
│   ├── csv_io.py          # locked atomic CSV I/O (CsvTable.transaction)
│   ├── atomic_write.py    # F-014: shared mkstemp+fsync+os.replace primitive
│   ├── bdv.py             # F-015: BDV_UNCLAIMED_RE / BDV_ANY_RE filename patterns
│   ├── phi_patterns.py    # F-005: shared PHI regexes (mrn/ssn/date/name/phone/address)
│   ├── phi_redact.py      # redact_field (presentation) + scrub_text (persistence)
│   ├── audit.py           # JSONL audit logger (log_audit)
│   ├── paths.py           # NAS path resolution (nas_root + resolve_paths)
│   ├── ffmpeg.py          # ffmpeg_concat, ffmpeg_deid, ffprobe helpers
│   ├── diagnostician.py   # qwen3:32b Ollama harness for verify (timeouts per F-001/F-002)
│   └── commands/          # one module per subcommand
│       ├── _shared.py     # F-033: format_cli_error helper
│       └── (concat, deid, verify, status, metadata)
├── app/                   # FastAPI + Gradio surgeon/admin app + Q3 worker
│   ├── main.py            # FastAPI app; mounts surgeon + admin Gradio apps; /login + /logout
│   ├── auth.py            # DSM auth + signed cookie (Fernet-wrapped partial-auth per F-008)
│   ├── surgeon_app.py     # Surgeon Gradio Blocks (Intake / My Cases / Action Required)
│   ├── admin_app.py       # Admin Gradio Blocks (Global Dashboard / Action Required) — Brief #4
│   ├── attention_actions.py # Type → action / display map (incl. malformed_marker display)
│   ├── scopes.py          # UserScope / SurgeonScope / AdminScope
│   ├── exceptions.py      # ScopeViolationError, MultipleClaimsError (Brief #4)
│   ├── phi.py             # Intake-time PHI scanner + format_phi_details (Brief #3.5b)
│   ├── repos/             # Case / Segment / Picklist / PipelineState / Attention / Manifest
│   ├── intake/            # Surgeon intake validation + submit handler
│   ├── worker/            # Q3 decoupled worker (lockfile/scan/dispatch/phi_scan/failures/main)
│   └── db/                # SQLite app.db (gitignored) + admin CLI + init_db + migrate_brief_4
├── tests/                 # 1090 tests covering all of the above (post Brief #4)
├── deploy/systemd/        # Worker systemd unit + timer templates
├── bench/                 # model benchmark harness (pre-existing)
├── scripts/               # gitignored — local-only operator scripts (migrate_manifest_spec_j etc.)
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
python -m pipeline metadata UCD-FIL-001                                              # show all 10 fields + stage (notes redacted per F-004)
python -m pipeline metadata UCD-FIL-001 --edit procedure_primary "Sigmoidectomy"     # dry-run preview
python -m pipeline metadata UCD-FIL-001 --edit procedure_primary "Sigmoidectomy" --confirm  # commit
# Note: --edit notes is operator-blocked (F-003); use the surgeon UI instead.

# Frame extraction (example)
python scripts/extract_frames.py --config configs/cholec80_frames.yaml

# Training (example)
python scripts/train.py --config configs/phase_recognition_v1.yaml

# Evaluation (example)
python scripts/evaluate.py --config configs/eval_cholec80.yaml
```
