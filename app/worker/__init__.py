"""Q3 decoupled worker.

Bridges the surgeon Intake submit (``.ready-<id>.json`` markers dropped by
``CaseRepository.submit_case``) to the pipeline CLI (``concat`` → ``deid``
→ ``verify``).

Designed for a systemd timer every 5 minutes invoking ``python -m app.worker
--once``. Daemon mode (``--daemon --interval N``) exists for local dev /
operator-side smoke; production uses the timer + --once pairing.

Module layout:
  lockfile.py — single-worker fcntl lock
  scan.py     — marker discovery + parsing + surgeon-folder validation
  dispatch.py — per-marker stage driver (concat → deid → verify)
  failures.py — attention_items writer + marker archival
  main.py     — argparse + iteration orchestrator
"""
