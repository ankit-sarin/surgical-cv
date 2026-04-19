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

## OR Video Pipeline (Two-Pass BDV Workflow)

Video arrives from the STERIS/BDV recording system as multiple ~2GB segments
per case (naming convention: `capt0_YYYYMMDD-HHMMSS.mp4`). Multi-segment input
is the standard, not an edge case. Processing uses two global Claude Code skills.

### Study Code Convention
Format: `UCD-FIL-###` (assigned in REDCap). The MRN ↔ study code mapping exists
only in REDCap. Study codes never appear in code, configs, or logs alongside
identifiable information.

### Pass 1: Concat segments → PHI master
- **Input:** BDV segments in `/mnt/nas/raw-[surgeon]/`
- **Tool:** `/video-concat`
- **Output:** `/mnt/nas/raw-[surgeon]/UCD-FIL-###_raw.mp4`
- **Verify:** Output duration ≈ sum of segment durations
- **Cleanup:** Delete original segments after concat verification (user-confirmed)

### Pass 2: De-identify PHI master → research copy
- **Input:** `/mnt/nas/raw-[surgeon]/UCD-FIL-###_raw.mp4`
- **Tool:** `/video-deidentify`
- **Output:** `/mnt/nas/deid-[surgeon]/UCD-FIL-###_video.mp4`
- **Verify:** ffprobe (no audio), exiftool (no metadata), visual spot-check
- **Cleanup:** PHI master retention policy TBD

### File Naming Convention
| Pattern | Usage |
|---------|-------|
| `UCD-FIL-###_raw.mp4` | PHI master (concat, pre-deid) in raw-[surgeon] |
| `UCD-FIL-###_video.mp4` | De-identified full video in deid-[surgeon] |
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

## Project Structure
```
surgical-cv/
├── CLAUDE.md              # Static architecture (this file)
├── primer.md              # Working state (maintained by Claude Code)
├── requirements.txt
├── pyproject.toml         # pytest config
├── src/
│   ├── deid/              # FFmpeg de-identification pipeline (raw → deid)
│   ├── data/              # Dataset loaders, video readers, frame samplers
│   ├── models/            # Model definitions (phase recognition, detection)
│   ├── training/          # Training loops, loss functions, schedulers
│   ├── evaluation/        # Metrics, visualization, benchmark runners
│   └── utils/             # FFmpeg wrappers, annotation converters, misc
├── configs/               # Experiment configs (YAML)
├── notebooks/             # Exploratory analysis, visualization
├── scripts/               # Data prep, training launchers, eval scripts
├── tests/                 # Unit and integration tests
└── data/                  # gitignored — local caches, extracted frames, annotations
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
- **PyTorch:** CPU-only or ONNX Runtime until Blackwell sm_121 gets PyTorch CUDA support
- Wrap all `ollama.chat()` calls in isolated methods for future TensorRT-LLM swap

## Key Patterns
- Experiment configs in YAML — one file per experiment, reproducible
- All video paths are NAS references, never local copies
- Frame extraction is a preprocessing step, cached in data/ (gitignored)
- Public dataset loaders should handle Cholec80/CholecT50 directory conventions
- Cross-family model verification for any LLM-assisted annotation
- De-identification pipeline: concat in raw-[surgeon] (read-write), then de-identify to deid-[surgeon] (read-write). Uses /video-concat and /video-deidentify global skills.

## Running
```bash
# Activate venv
source venv/bin/activate

# Tests
python -m pytest tests/ -v
python -m pytest tests/ -v -m "not slow"    # skip long-running tests

# De-identification — use Claude Code with global skills:
# "Concat the segments in raw-miller and de-identify to deid-miller as UCD-FIL-002"
# This triggers /video-concat (pass 1) then /video-deidentify (pass 2)

# Frame extraction (example)
python scripts/extract_frames.py --config configs/cholec80_frames.yaml

# Training (example)
python scripts/train.py --config configs/phase_recognition_v1.yaml

# Evaluation (example)
python scripts/evaluate.py --config configs/eval_cholec80.yaml
```
