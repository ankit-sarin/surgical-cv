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

### Raw OR Video (PHI — read-only)
| Path | Contents |
|------|----------|
| /mnt/nas/or-raw/ | Raw OR video (all surgeons, PHI) |
| /mnt/nas/raw-sarin/ | Raw video — Sarin cases |
| /mnt/nas/raw-miller/ | Raw video — Miller cases |

### De-identified Output (read-write)
| Path | Contents |
|------|----------|
| /mnt/nas/deid-sarin/ | De-identified video — Sarin cases |
| /mnt/nas/deid-miller/ | De-identified video — Miller cases |

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
- De-identification pipeline reads from raw (read-only) and writes to deid (read-write)

## Running
```bash
# Activate venv
source venv/bin/activate

# Tests
python -m pytest tests/ -v
python -m pytest tests/ -v -m "not slow"    # skip long-running tests

# De-identification (example)
python scripts/deid_video.py --input /mnt/nas/raw-sarin/case_001.mp4 --output /mnt/nas/deid-sarin/

# Frame extraction (example)
python scripts/extract_frames.py --config configs/cholec80_frames.yaml

# Training (example)
python scripts/train.py --config configs/phase_recognition_v1.yaml

# Evaluation (example)
python scripts/evaluate.py --config configs/eval_cholec80.yaml
```
