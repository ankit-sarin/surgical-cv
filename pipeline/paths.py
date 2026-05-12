import os
from dataclasses import dataclass
from pathlib import Path


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


def resolve_paths(root: Path | str | None = None) -> NasPaths:
    if root is None:
        env_root = os.environ.get("PIPELINE_NAS_ROOT")
        root = env_root if env_root else "/mnt/nas"
    root_path = Path(root)
    or_raw = root_path / "or-raw"
    return NasPaths(
        root=root_path,
        or_raw=or_raw,
        state_csv=or_raw / "pipeline_state.csv",
        manifest_csv=or_raw / "case_manifest.csv",
        audit_log=or_raw / "pipeline.log",
    )
