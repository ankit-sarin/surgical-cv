from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CASE_MANIFEST_COLUMNS: tuple[str, ...] = (
    "ucd_fil_id",
    "surgeon",
    "case_year",
    "or_room",
    "procedure_name",
    "approach",
    "indication",
    "notes",
)

PIPELINE_STATE_COLUMNS: tuple[str, ...] = (
    "ucd_fil_id",
    "raw_segments",
    "concat_filename",
    "deid_filename",
    "stage",
    "concat_ts",
    "deid_ts",
    "verify_ts",
    "verification_notes",
)


class Stage(str, Enum):
    intake = "intake"
    concatenated = "concatenated"
    deidentified = "deidentified"
    verified = "verified"
    failed = "failed"


_ALLOWED_TRANSITIONS: frozenset[tuple["Stage", "Stage"]] = frozenset(
    {
        (Stage.intake, Stage.concatenated),
        (Stage.concatenated, Stage.deidentified),
        (Stage.deidentified, Stage.verified),
        (Stage.deidentified, Stage.failed),
        (Stage.concatenated, Stage.failed),
        (Stage.intake, Stage.failed),
        (Stage.failed, Stage.deidentified),
    }
)


def is_valid_transition(from_stage: Stage, to_stage: Stage) -> bool:
    return (from_stage, to_stage) in _ALLOWED_TRANSITIONS


class CaseManifestRow(BaseModel):
    ucd_fil_id: str = Field(pattern=r"^UCD-FIL-\d{3}$")
    surgeon: str = Field(min_length=1)
    case_year: str = Field(pattern=r"^\d{4}$")
    or_room: str = Field(min_length=1)
    procedure_name: str = Field(min_length=1)
    approach: str = Field(min_length=1)
    indication: str = Field(min_length=1)
    notes: str = ""

    @field_validator("surgeon")
    @classmethod
    def _surgeon_lowercase_no_whitespace(cls, v: str) -> str:
        if v != v.lower():
            raise ValueError("surgeon must be lowercase")
        if any(c.isspace() for c in v):
            raise ValueError("surgeon must not contain whitespace")
        return v

    @classmethod
    def from_csv_dict(cls, d: dict) -> "CaseManifestRow":
        return cls(**{col: d.get(col, "") for col in CASE_MANIFEST_COLUMNS})

    def to_csv_dict(self) -> dict:
        return {col: getattr(self, col) for col in CASE_MANIFEST_COLUMNS}


class PipelineStateRow(BaseModel):
    ucd_fil_id: str = Field(pattern=r"^UCD-FIL-\d{3}$")
    raw_segments: list[str] = Field(min_length=1)
    concat_filename: str = ""
    deid_filename: str = ""
    stage: Stage
    concat_ts: str = ""
    deid_ts: str = ""
    verify_ts: str = ""
    verification_notes: str = ""

    @field_validator("raw_segments")
    @classmethod
    def _segments_pipe_safe(cls, v: list[str]) -> list[str]:
        for seg in v:
            if not seg:
                raise ValueError("raw_segments elements must be non-empty")
            if "|" in seg:
                raise ValueError("raw_segments elements must not contain '|'")
        return v

    @field_validator("concat_ts", "deid_ts", "verify_ts")
    @classmethod
    def _iso_or_empty(cls, v: str) -> str:
        if v == "":
            return v
        try:
            datetime.fromisoformat(v)
        except ValueError as e:
            raise ValueError(f"invalid ISO 8601 timestamp: {v!r}") from e
        return v

    @classmethod
    def from_csv_dict(cls, d: dict) -> "PipelineStateRow":
        payload = {col: d.get(col, "") for col in PIPELINE_STATE_COLUMNS}
        segs = payload["raw_segments"]
        if isinstance(segs, str):
            payload["raw_segments"] = segs.split("|") if segs else []
        return cls(**payload)

    def to_csv_dict(self) -> dict:
        out: dict = {}
        for col in PIPELINE_STATE_COLUMNS:
            val = getattr(self, col)
            if col == "raw_segments":
                out[col] = "|".join(val)
            elif col == "stage":
                out[col] = val.value
            else:
                out[col] = val
        return out


class DiagnosticianVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["pass", "fail"]
    reason: str = Field(min_length=1, max_length=200)
    evidence: list[str] = Field(max_length=10)

    @field_validator("evidence")
    @classmethod
    def _evidence_elements_non_empty(cls, v: list[str]) -> list[str]:
        for e in v:
            if not e:
                raise ValueError("evidence elements must be non-empty")
        return v
