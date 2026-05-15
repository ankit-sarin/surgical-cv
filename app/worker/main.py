"""Worker entry point — ``python -m app.worker [--once|--daemon]``.

Production deployment is a systemd timer firing ``--once`` every 5 minutes
(templates in ``deploy/systemd/``). ``--daemon`` exists for local dev and
operator-side smokes; both modes share the single-iteration body."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from pipeline.paths import NasPaths, resolve_paths

from app.worker.dispatch import (
    DispatchOutcome,
    PipelineDriver,
    SubprocessPipelineDriver,
    dispatch_marker,
)
from app.worker.failures import (
    ensure_system_worker_user,
    record_dispatch_outcome,
    record_malformed,
)
from app.worker.lockfile import (
    WorkerAlreadyRunningError,
    default_lock_path,
    single_worker_lock,
)
from app.worker.scan import (
    Marker,
    MalformedMarker,
    parse_marker,
    scan_markers,
)

_DEFAULT_INTERVAL_SECONDS = 60

log = logging.getLogger("app.worker")


def _build_logger() -> None:
    """Stdout-only logging — systemd journal captures it. No file handler /
    rotation; the worker is short-lived per --once invocation."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    # Idempotent — re-running tests in-process shouldn't stack handlers.
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def run_iteration(
    paths: NasPaths, driver: PipelineDriver
) -> dict[str, int]:
    """Process every marker currently in the surgeons' raw folders, FIFO.
    Returns a summary dict (counts by outcome kind) — useful for tests and
    for the daemon's per-iteration log line."""
    counts = {
        "success": 0,
        "soft_fail": 0,
        "hard_fail": 0,
        "orphan": 0,
        "malformed": 0,
    }
    for marker_path in scan_markers(paths.root):
        result = parse_marker(marker_path)
        if isinstance(result, MalformedMarker):
            log.warning(
                "marker=%s malformed: %s", result.path, result.reason
            )
            record_malformed(result)
            counts["malformed"] += 1
            continue

        marker: Marker = result
        log.info(
            "dispatching marker=%s case=%s surgeon=%s segments=%d",
            marker.path,
            marker.ucd_fil_id,
            marker.surgeon,
            len(marker.segments),
        )
        outcome: DispatchOutcome = dispatch_marker(marker, paths, driver)
        log.info(
            "outcome marker=%s case=%s kind=%s stage=%s rc=%s",
            marker.path,
            marker.ucd_fil_id,
            outcome.kind,
            outcome.stage,
            outcome.returncode,
        )
        record_dispatch_outcome(marker, outcome)
        counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
    return counts


def _parse_argv(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="app.worker",
        description="Q3 decoupled worker: drives .ready-*.json markers "
        "through the pipeline CLI.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once", action="store_true",
        help="Run a single iteration and exit. Pairs with a systemd timer.",
    )
    mode.add_argument(
        "--daemon", action="store_true",
        help="Long-running loop. Holds the lock for the process lifetime.",
    )
    p.add_argument(
        "--interval", type=int, default=_DEFAULT_INTERVAL_SECONDS,
        help=f"Daemon loop interval in seconds (default {_DEFAULT_INTERVAL_SECONDS}). Ignored in --once mode.",
    )
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    paths: NasPaths | None = None,
    driver: PipelineDriver | None = None,
    sleep_fn=time.sleep,
) -> int:
    """Process entry point. ``paths``, ``driver``, and ``sleep_fn`` are
    injection seams for tests; production callers pass nothing and get the
    real NAS + subprocess driver."""
    _build_logger()
    args = _parse_argv(argv)

    paths = paths if paths is not None else resolve_paths()
    driver = driver if driver is not None else SubprocessPipelineDriver()

    ensure_system_worker_user()

    lock_path = default_lock_path(paths.root)
    try:
        with single_worker_lock(lock_path):
            if args.once:
                counts = run_iteration(paths, driver)
                log.info("iteration complete: %s", counts)
                return 0
            # Daemon mode: loop until killed.
            log.info(
                "starting daemon mode; interval=%ds lock=%s",
                args.interval, lock_path,
            )
            while True:
                counts = run_iteration(paths, driver)
                log.info("iteration complete: %s", counts)
                sleep_fn(args.interval)
    except WorkerAlreadyRunningError as e:
        log.error("worker already running: %s", e)
        return 2
    except KeyboardInterrupt:
        log.info("daemon stopped by signal")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
