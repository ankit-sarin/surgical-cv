Read primer.md for current project state before starting work.

# Surgical Computer Vision (Project 5)

## Location
~/projects/surgical-cv

## Deployment
None. This is research code — no systemd services, no Gradio apps, no Cloudflare tunnels.

## Purpose
Computer vision research on surgical video: phase recognition, instrument detection, and dataset curation. Targets ~3 publications: (1) dataset/benchmark paper, (2) phase recognition model, (3) clinical application (AI-assisted video editing).

## PI & Context
- **PI:** Ankit Sarin, MD — Chief, Colorectal Surgery, UC Davis Health
- **Lab:** PLUM Lab collaboration
- **IRB:** Approved; de-identification SOP in place
- **Hardware:** NVIDIA DGX Spark (Blackwell GB10, 128GB unified memory)

## Data Sources
| Path | Contents |
|------|----------|
| /mnt/nas/deid-sarin/ | De-identified OR video (PI's cases) |
| /mnt/nas/deid-shared/ | De-identified OR video (shared) |
| /mnt/nas/research-datasets/ | Public datasets (Cholec80, CholecT50, etc.) |

All video is accessed read-only via NFS from the STRIVE NAS. Raw video never copied into this repo.

## Project Structure
```
surgical-cv/
├── CLAUDE.md              # Static architecture (this file)
├── primer.md              # Working state (maintained by Claude Code)
├── requirements.txt
├── pyproject.toml         # pytest config
├── src/
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

## Scope Boundaries
**In scope:**
- Video frame extraction and preprocessing (FFmpeg)
- Surgical phase recognition models
- Instrument detection models
- Dataset curation tooling (annotation, splits, statistics)
- Benchmark evaluation against public datasets
- Publication figures and tables

**Out of scope (lives elsewhere):**
- De-identification pipeline (~/scripts/ or separate project)
- Gradio apps or web UIs
- Cloudflare deployments
- RAG pipelines
- Operative report generation (~/projects/operativereports/)

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

## Running
```bash
# Activate venv
source venv/bin/activate

# Tests
python -m pytest tests/ -v
python -m pytest tests/ -v -m "not slow"    # skip long-running tests

# Frame extraction (example)
python scripts/extract_frames.py --config configs/cholec80_frames.yaml

# Training (example)
python scripts/train.py --config configs/phase_recognition_v1.yaml

# Evaluation (example)
python scripts/evaluate.py --config configs/eval_cholec80.yaml
```
