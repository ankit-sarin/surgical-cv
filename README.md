# pipeline

A deterministic Python CLI that orchestrates the surgical OR video processing
workflow: concatenate BDV segments into a PHI master, de-identify the master
into a research copy, track state across cases in locked CSVs, and emit a
forensic audit log. Run it from the project root with the project venv active:

```bash
python -m pipeline --help
```

## Subcommands

| Command | Purpose |
|---|---|
| `pipeline concat --surgeon <name>` | Batch-concatenate `intake` cases for one surgeon. |
| `pipeline deid --surgeon <name> [--case UCD-FIL-###]` | De-identify `concatenated` cases (batch or single-case). |
| `pipeline status [--case ...] [--stage ...] [--json]` | Read-only joined view of manifest + state. |
| `pipeline verify <deid_file>` | _stub — not yet wired_ |
| `pipeline metadata UCD-FIL-### [--show|--edit FIELD VALUE --confirm]` | _stub — not yet wired_ |

## State files (under `/mnt/nas/or-raw/`)

| File | Owner | Purpose |
|---|---|---|
| `case_manifest.csv` | surgeon-entered (today: `scripts/seed_intake.py`; future: Gradio) | Per-case metadata (procedure, approach, indication, …). |
| `pipeline_state.csv` | DGX pipeline | Per-case stage + filenames + timestamps. |
| `pipeline.log` | DGX pipeline | JSONL audit trail, one entry per subcommand invocation. |

CSV writes are atomic (write to `.tmp`, fsync, `os.replace`) and serialized by
sibling `.lock` files using `fcntl.LOCK_EX`. State reads are unlocked
snapshots — a reader always sees either the pre- or post-rename file, never a
torn write.

## Stage machine

```
intake → concatenated → deidentified → verified
   ↓           ↓             ↓
 failed     failed        failed → deidentified  (retry path)
```

## Tests

```bash
.venv/bin/pytest -q
```
