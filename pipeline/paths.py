"""NAS path resolution — single source of truth for the surgical-cv codebase.

``PIPELINE_NAS_ROOT`` is the only env var that selects the NAS root. Both the
pipeline CLI (via ``resolve_paths``) and the app layer (via
``app.repos.segments.raw_root``, which now delegates here) read from this
single function. F-012 retired the parallel ``RAW_VIDEO_ROOT`` env var to
remove the silent-drift failure mode where the marker writer and the worker
scanner could land in different folders.
"""

import os
from dataclasses import dataclass
from pathlib import Path


_DEFAULT_NAS_ROOT = Path("/mnt/nas")


@dataclass(frozen=True)
class NasPaths:
    root: Path
    or_raw: Path
    state_csv: Path
    manifest_csv: Path
    audit_log: Path

    def raw_dir(self, surgeon: str) -> Path:
        return self.root / f"raw-{surgeon}"

    def deid_dir(self, surgeon: str) -> Path:
        return self.root / f"deid-{surgeon}"


def nas_root() -> Path:
    """Resolve the NAS root from ``PIPELINE_NAS_ROOT`` (env) or fall back to
    ``/mnt/nas``. Single source of truth — every other layer (segments repo,
    cases repo's marker writer, worker scanner) reads from here so an
    operator who sets the env var once gets consistent behavior across the
    FastAPI service and the worker process."""
    env = os.environ.get("PIPELINE_NAS_ROOT")
    if env:
        return Path(env)
    return _DEFAULT_NAS_ROOT


def resolve_paths(root: Path | str | None = None) -> NasPaths:
    if root is None:
        root_path = nas_root()
    else:
        root_path = Path(root)
    or_raw = root_path / "or-raw"
    return NasPaths(
        root=root_path,
        or_raw=or_raw,
        state_csv=or_raw / "pipeline_state.csv",
        manifest_csv=or_raw / "case_manifest.csv",
        audit_log=or_raw / "pipeline.log",
    )
