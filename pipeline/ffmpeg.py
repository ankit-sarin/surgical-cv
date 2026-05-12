import json
import re
import subprocess
import tempfile
from pathlib import Path


_BDV_FILENAME_RE = re.compile(r"^capt0_(\d{8}-\d{6})(?:-copied)?\.mp4$")


class FFmpegToolError(Exception):
    pass


class FFprobeError(FFmpegToolError):
    def __init__(self, path: Path | None, stderr: str):
        self.path = path
        self.stderr = stderr
        if path is None:
            super().__init__(stderr)
        else:
            super().__init__(f"ffprobe failed for {path}: {stderr}")


class CodecMismatchError(FFmpegToolError):
    def __init__(
        self,
        reference_path: Path,
        mismatched_path: Path,
        ref_signature: tuple,
        mismatched_signature: tuple,
    ):
        self.reference_path = reference_path
        self.mismatched_path = mismatched_path
        self.ref_signature = ref_signature
        self.mismatched_signature = mismatched_signature
        super().__init__(
            f"Codec mismatch: {mismatched_path} {mismatched_signature} "
            f"differs from reference {reference_path} {ref_signature}"
        )


class FFmpegError(FFmpegToolError):
    def __init__(self, stderr: str, exit_code: int):
        self.stderr = stderr
        self.exit_code = exit_code
        super().__init__(f"ffmpeg failed (exit {exit_code}): {stderr}")


class BdvFilenameError(FFmpegToolError):
    def __init__(self, filename: str):
        self.filename = filename
        super().__init__(f"Filename does not match BDV pattern: {filename!r}")


def ffprobe_streams(path: Path) -> dict:
    argv = ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFprobeError(path, result.stderr.strip())
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise FFprobeError(path, f"unparseable ffprobe JSON: {e}") from e


def video_stream_signature(probe: dict) -> tuple[str, int, int, str, str]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return (
                stream["codec_name"],
                stream["width"],
                stream["height"],
                stream["avg_frame_rate"],
                stream["pix_fmt"],
            )
    raise FFprobeError(None, "no video stream found in probe result")


def check_uniformity(paths: list[Path]) -> None:
    if not paths:
        raise ValueError("check_uniformity requires at least one path")
    if len(paths) == 1:
        ffprobe_streams(paths[0])
        return
    ref_probe = ffprobe_streams(paths[0])
    ref_sig = video_stream_signature(ref_probe)
    for p in paths[1:]:
        probe = ffprobe_streams(p)
        sig = video_stream_signature(probe)
        if sig != ref_sig:
            raise CodecMismatchError(
                reference_path=paths[0],
                mismatched_path=p,
                ref_signature=ref_sig,
                mismatched_signature=sig,
            )


def parse_bdv_timestamp(filename: str) -> str:
    """Parse the timestamp out of a BDV recorder filename.

    Accepts both the canonical form (``capt0_YYYYMMDD-HHMMSS.mp4``) and the
    ``-copied`` variant (``capt0_YYYYMMDD-HHMMSS-copied.mp4``), returning the
    same timestamp string in both cases. The ``-copied`` form is what STERIS
    writes after duplicating a segment, so callers reasoning about already-
    moved segments can still recover the original timestamp.
    """
    m = _BDV_FILENAME_RE.match(filename)
    if m is None:
        raise BdvFilenameError(filename)
    return m.group(1)


def _escape_concat_path(p: str) -> str:
    return p.replace("'", "'\\''")


def ffmpeg_deid(input_path: Path, output_path: Path) -> None:
    """De-identify a video by stripping audio, clearing metadata, and
    re-encoding video at visually-lossless quality.

    Flags:
        -an: drop audio entirely (PHI — OR team voices).
        -map_metadata -1: clear container-level metadata (patient name,
            MRN, dates embedded by Stryker / Karl Storz / BDV).
        -c:v libx264 -crf 18: re-encode at visually-lossless quality —
            necessary to clear stream-level headers that may carry PHI
            even after container metadata is cleared.
        -movflags +faststart: move the moov atom to the front, enabling
            progressive playback (important for collaborator review).
    """
    input_path = Path(input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"input is not a file: {input_path}")
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(f"output already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(input_path),
        "-an",
        "-map_metadata", "-1",
        "-c:v", "libx264",
        "-crf", "18",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFmpegError(
            stderr=result.stderr.strip(), exit_code=result.returncode
        )


def ffmpeg_concat(segments: list[Path], output: Path) -> None:
    if not segments:
        raise ValueError("ffmpeg_concat requires at least one segment")
    for seg in segments:
        if not Path(seg).is_file():
            raise FileNotFoundError(f"segment is not a file: {seg}")
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    list_fd, list_path_str = tempfile.mkstemp(
        prefix=output.name + ".concatlist.",
        suffix=".txt",
        dir=str(output.parent),
    )
    list_path = Path(list_path_str)
    try:
        with open(list_fd, "w") as f:
            for seg in segments:
                escaped = _escape_concat_path(str(Path(seg).resolve()))
                f.write(f"file '{escaped}'\n")
        argv = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output),
        ]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            raise FFmpegError(stderr=result.stderr.strip(), exit_code=result.returncode)
    finally:
        try:
            list_path.unlink()
        except FileNotFoundError:
            pass
