import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# F-016: single source of truth for the UCD-FIL-### case-id pattern. Both
# the schema validators (CaseManifestRow, PipelineStateRow) and every CLI /
# repo / worker module that needed to recognize a case id used to redefine
# this regex independently, with at least one drift (cases.py used \d{3,}
# which would have allowed UCD-FIL-1000 — incompatible with the schema's
# strict \d{3}). Tightened to \d{3} everywhere; the lenient form was a
# latent bug, not a feature.
CASE_ID_RE_STR: str = r"^UCD-FIL-\d{3}$"
CASE_ID_RE: re.Pattern[str] = re.compile(CASE_ID_RE_STR)

# F-017: surgeon folder-name pattern, replicated across concat / deid /
# verify CLI handlers. Hoisted here so a future surgeon onboarding (e.g.,
# a name with characters outside [a-z0-9-]) only needs one update.
SURGEON_RE_STR: str = r"^[a-z][a-z0-9-]*$"
SURGEON_RE: re.Pattern[str] = re.compile(SURGEON_RE_STR)

# F-017: max length of the verification_notes column. Three identical
# definitions across concat/deid/verify CLI commands; consolidated here.
VERIFICATION_NOTES_MAX: int = 200


CASE_MANIFEST_COLUMNS: tuple[str, ...] = (
    "ucd_fil_id",
    "surgeon",
    "case_year",
    "or_room",
    "procedure_primary",
    "procedure_additional",
    "approach",
    "conversion_target",
    "indication",
    "notes",
)

PIPELINE_STATE_COLUMNS: tuple[str, ...] = (
    "ucd_fil_id",
    "raw_segments",
    "concat_filename",
    "deid_filename",
    "stage",
    "intake_ts",
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
    ucd_fil_id: str = Field(pattern=CASE_ID_RE_STR)
    surgeon: str = Field(min_length=1)
    case_year: str = Field(pattern=r"^\d{4}$")
    or_room: str = Field(min_length=1)
    procedure_primary: str = Field(min_length=1)
    # JSON-encoded array of strings on disk; surfaced as list[str] in memory.
    # Empty disk value ↔ [].
    procedure_additional: list[str] = Field(default_factory=list)
    approach: str = Field(min_length=1)
    # Empty string on disk and in memory = "no conversion".
    conversion_target: str = ""
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

    @field_validator("procedure_additional")
    @classmethod
    def _additionals_nonempty_strings(cls, v: list[str]) -> list[str]:
        for s in v:
            if not isinstance(s, str) or not s:
                raise ValueError(
                    "procedure_additional elements must be non-empty strings"
                )
        return v

    @classmethod
    def from_csv_dict(cls, d: dict) -> "CaseManifestRow":
        payload = {col: d.get(col, "") for col in CASE_MANIFEST_COLUMNS}
        raw_additional = payload["procedure_additional"]
        if isinstance(raw_additional, str):
            if raw_additional == "":
                payload["procedure_additional"] = []
            else:
                try:
                    parsed = json.loads(raw_additional)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"procedure_additional is not valid JSON: {e}"
                    ) from e
                if not isinstance(parsed, list):
                    raise ValueError(
                        "procedure_additional must be a JSON array, "
                        f"got {type(parsed).__name__}"
                    )
                payload["procedure_additional"] = parsed
        return cls(**payload)

    def to_csv_dict(self) -> dict:
        out: dict = {}
        for col in CASE_MANIFEST_COLUMNS:
            val = getattr(self, col)
            if col == "procedure_additional":
                # Empty list rounds to empty string on disk (readability win);
                # any non-empty list serializes as a compact JSON array.
                out[col] = "" if not val else json.dumps(val)
            else:
                out[col] = val
        return out


class PipelineStateRow(BaseModel):
    ucd_fil_id: str = Field(pattern=CASE_ID_RE_STR)
    raw_segments: list[str] = Field(min_length=1)
    concat_filename: str = ""
    deid_filename: str = ""
    stage: Stage
    # Submission timestamp: copied from the worker marker's ``submitted_at``
    # at intake-row creation. Empty for rows written before this column
    # existed; downstream "stuck submission" detection treats empty as
    # unknown rather than escalating.
    intake_ts: str = ""
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

    @field_validator("intake_ts", "concat_ts", "deid_ts", "verify_ts")
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
