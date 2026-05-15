"""LLM diagnostician for de-identified surgical video.

Collects evidence from the deid artifact (ffprobe + exiftool + ffmpeg null-mux
stderr), builds a single-shot prompt, calls qwen3:32b via Ollama with
format="json" and think=False, and validates the response against
DiagnosticianVerdict. One retry on parse/validation failure; no retry on
daemon-level failures (ConnectError, TimeoutException, ResponseError).
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import ollama
from pydantic import ValidationError

from pipeline.schemas import DiagnosticianVerdict


OLLAMA_MODEL = "qwen3:32b"
OLLAMA_TEMPERATURE = 0
OLLAMA_NUM_PREDICT = 512

# Wall-clock cap on a single diagnostician chat() call. The ollama-python client
# takes the timeout at Client construction (httpx-backed); there is no per-call
# kwarg on chat(). We construct a module-level Client with this timeout and
# route every call through it.
#
# Sizing: CLAUDE.md documents a 300 s per-call ceiling for qwen3:32b on the
# Blackwell GB10. Measured warm steady-state on a representative diagnostician
# prompt is ~14 s (probed 2026-05-15, model pinned). Cold-load could not be
# directly measured because qwen3:32b is currently pinned UNTIL=Forever; we
# allocate ~150 s of headroom on top of the documented 300 s ceiling to cover
# (a) cold reload after an OOM eviction or operator-initiated unload, and
# (b) GPU contention from a co-resident model. 450 s stays well under the
# systemd TimeoutStartSec=30min so multiple cases can still process per
# iteration if one runs long.
_OLLAMA_CHAT_TIMEOUT_S = 450

_FFPROBE_TIMEOUT_S = 60
_EXIFTOOL_TIMEOUT_S = 60
# Full-file decode pass for the null-mux. ~10× real-time on a ~60-min video
# at typical NFS read rates leaves comfortable headroom for slow NAS or a
# pathological codec path.
_NULLMUX_TIMEOUT_S = 600

# Module-level client so the timeout is applied to every chat() call without
# reconstruction overhead. ollama.Client wraps httpx; passing a float assigns
# it to all timeout phases (connect/read/write/pool).
_ollama_client = ollama.Client(timeout=_OLLAMA_CHAT_TIMEOUT_S)


@dataclass
class DiagnosticianInfraError(Exception):
    """Raised when the diagnostician cannot produce a verdict due to
    infrastructure failure (daemon unreachable, malformed output after retry).
    Distinct from a clean fail verdict, which is a normal product of the LLM.
    """

    reason: str
    error: str = ""
    raw_outputs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(f"diagnostician infrastructure error: {self.reason}")


def collect_evidence(deid_path: Path) -> dict[str, Any]:
    """Run three subprocess calls against the deid artifact and return their
    parsed outputs as a dict. Used both by the diagnostician prompt builder
    and by the deterministic preflight (which inspects the same ffprobe +
    exiftool data without making any LLM call).

    Returns a dict with keys:
        ffprobe: parsed JSON dict from `ffprobe -show_format -show_streams`
        exiftool: parsed dict from `exiftool -j` (index 0 of returned list)
        ffmpeg_stderr: combined stderr from null-mux decode pass (string)
    """
    deid_path = Path(deid_path)
    if not deid_path.is_file():
        raise FileNotFoundError(f"deid artifact not found: {deid_path}")

    try:
        ffprobe_result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(deid_path),
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffprobe timed out after {_FFPROBE_TIMEOUT_S}s on {deid_path}"
        ) from e
    if ffprobe_result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {deid_path}: {ffprobe_result.stderr.strip()}"
        )
    ffprobe_json = json.loads(ffprobe_result.stdout)

    try:
        exiftool_result = subprocess.run(
            ["exiftool", "-j", str(deid_path)],
            capture_output=True,
            text=True,
            timeout=_EXIFTOOL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"exiftool timed out after {_EXIFTOOL_TIMEOUT_S}s on {deid_path}"
        ) from e
    if exiftool_result.returncode != 0:
        raise RuntimeError(
            f"exiftool failed for {deid_path}: {exiftool_result.stderr.strip()}"
        )
    exiftool_parsed = json.loads(exiftool_result.stdout)
    exiftool_json: dict[str, Any] = (
        exiftool_parsed[0] if isinstance(exiftool_parsed, list) and exiftool_parsed
        else {}
    )

    try:
        null_mux = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-loglevel",
                "info",
                "-i",
                str(deid_path),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=_NULLMUX_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg null-mux timed out after {_NULLMUX_TIMEOUT_S}s on {deid_path}"
        ) from e

    return {
        "ffprobe": ffprobe_json,
        "exiftool": exiftool_json,
        "ffmpeg_stderr": null_mux.stderr,
    }


_PROMPT_TEMPLATE = """\
You are a HIPAA-compliance auditor for de-identified surgical video. You evaluate
the metadata signals of one MP4 file and decide whether it is safe to release
for research use.

## Corpus context

The video was sourced from a BDV recording system used during minimally invasive
abdominal surgery. The source files have these properties by design:
- No audio content (the recording system is silent by configuration)
- No burned-in patient identifiers on frames (no overlays present)

Re-identification risk lives at the container and stream-metadata layer, not in
pixel content or audio content.

## Expected de-identification operations

The deid pipeline should have, on the original PHI master:
- Stripped all audio streams (-an)
- Cleared all container and stream-level metadata (-map_metadata -1)
- Re-encoded video to fresh H.264 via libx264 at CRF 18
- Enabled MOV faststart
- Renamed the output to an opaque ID matching UCD-FIL-\\d{{3}}_video.mp4

## What you receive

Three evidence blocks describing the deid artifact's current state:
1. ffprobe — JSON dump of container format and streams
2. exiftool — JSON dump of file-level metadata
3. ffmpeg null-mux stderr — output of a decode pass that surfaces container parse
   output, codec detection, and bitrate confirmation

## Signals that constitute a FAIL

- Any audio stream present in the output
- Container-level metadata fields with PHI-shaped content: title, comment,
  artist, composer, creator, description, genre, location, GPS coordinates,
  original creation_time, patient/case identifiers, surgeon name, hospital name
- Stream-level metadata with PHI: stream title, handler_name containing PHI
  strings
- Encoder strings that reveal original source identity (e.g., Sony AVCHD,
  Olympus, BDV-specific tags). Note: Lavf*, Lavc*, libx264 are expected and
  ACCEPTABLE — they are the deid pipeline's own re-encode signature
- Filename pattern not matching UCD-FIL-\\d{{3}}_video.mp4
- Video codec is not h264 / libx264 (deid should have re-encoded;
  stream-copy passthrough is a deid failure)

## Signals that do NOT constitute a fail in this corpus

- Absence of audio (expected by source design — confirm it's absent, do not flag)
- Absence of overlay/subtitle streams (expected)
- File size larger than source (expected at CRF 18 on already-compressed source)
- creation_time absent (expected — should be cleared)

## Output

Return JSON exactly matching this schema. Do not return any text outside the
JSON. Do not wrap in markdown fences.

{{
  "verdict": "pass" | "fail",
  "reason": <string, 1-200 chars, single-sentence summary>,
  "evidence": <array of up to 10 short non-empty strings, each citing a
               specific observed field name and value>
}}

Be specific in "evidence": cite the actual field names and values you observed.
For a pass verdict, evidence should cite the positive signals (e.g., "zero
audio streams", "format.tags.encoder=Lavf60.16.100", "opaque filename
UCD-FIL-XXX_video.mp4", "video codec=h264"). For a fail, cite the specific
PHI signal observed with its value.

---

## ffprobe

```json
{ffprobe_json}
```

## exiftool

```json
{exiftool_json}
```

## ffmpeg stderr

```
{ffmpeg_stderr}
```
"""


def build_prompt(evidence: dict[str, Any]) -> str:
    return _PROMPT_TEMPLATE.format(
        ffprobe_json=json.dumps(evidence.get("ffprobe", {}), indent=2),
        exiftool_json=json.dumps(evidence.get("exiftool", {}), indent=2),
        ffmpeg_stderr=evidence.get("ffmpeg_stderr", ""),
    )


def _call_ollama(prompt: str) -> str:
    """One Ollama call. Returns the raw content string. Re-raises infra
    exceptions (httpx.ConnectError, ollama.ResponseError) for the caller to
    translate into DiagnosticianInfraError.

    Wall-clock timeout: enforced by the module-level ``_ollama_client`` (see
    ``_OLLAMA_CHAT_TIMEOUT_S``). On expiry httpx raises ``TimeoutException``,
    which we re-raise as ``RuntimeError`` here so verify's generic exception
    handler records it as a per-case stage failure rather than letting the
    DiagnosticianInfraError "ollama_unavailable" path abort the entire batch.
    A single slow case should not stall every other case in the iteration.
    """
    try:
        response = _ollama_client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            format="json",
            options={
                "temperature": OLLAMA_TEMPERATURE,
                "num_predict": OLLAMA_NUM_PREDICT,
            },
            think=False,
        )
    except httpx.TimeoutException as e:
        raise RuntimeError(
            f"diagnostician timed out after {_OLLAMA_CHAT_TIMEOUT_S}s"
        ) from e
    return response["message"]["content"]


def _parse_verdict(raw: str) -> DiagnosticianVerdict:
    """Parse and validate one raw response. Raises json.JSONDecodeError or
    pydantic.ValidationError on malformed input — the caller decides whether
    to retry.
    """
    parsed = json.loads(raw)
    return DiagnosticianVerdict.model_validate(parsed)


def diagnose(evidence: dict[str, Any]) -> DiagnosticianVerdict:
    """Single-shot diagnosis with one retry on parse/validation failure.

    Retry policy:
      - json.JSONDecodeError or pydantic.ValidationError: retry once with
        identical prompt. On second failure, raise DiagnosticianInfraError
        with reason="malformed_output".
      - httpx.ConnectError, ollama.ResponseError: no retry. Raise
        DiagnosticianInfraError with reason="ollama_unavailable".
      - httpx.TimeoutException: caught inside ``_call_ollama`` (F-001) and
        re-raised as ``RuntimeError("diagnostician timed out after Ns")`` so
        verify's generic exception handler records a per-case stage failure
        instead of aborting the batch via the ollama-unavailable path.
    """
    prompt = build_prompt(evidence)
    raw_outputs: list[str] = []

    for attempt in (1, 2):
        try:
            raw = _call_ollama(prompt)
        except (httpx.ConnectError, ollama.ResponseError) as e:
            # F-031: ``httpx.TimeoutException`` removed from this tuple — it
            # was structurally unreachable after F-001 made ``_call_ollama``
            # catch and re-raise it as ``RuntimeError`` before it could
            # propagate here.
            raise DiagnosticianInfraError(
                reason="ollama_unavailable", error=str(e)
            ) from e

        raw_outputs.append(raw)
        try:
            return _parse_verdict(raw)
        except (json.JSONDecodeError, ValidationError):
            if attempt == 2:
                raise DiagnosticianInfraError(
                    reason="malformed_output", raw_outputs=raw_outputs
                )

    # Unreachable — loop always returns or raises.
    raise DiagnosticianInfraError(
        reason="malformed_output", raw_outputs=raw_outputs
    )
