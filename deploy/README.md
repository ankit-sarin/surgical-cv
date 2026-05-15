# Surgical-CV deployment templates

This directory holds infrastructure templates. **No file here is installed
automatically** — installation is a manual operator step. Spec L explicitly
keeps `systemctl` invocations out of the worker spec.

## Files

| File | Purpose |
|---|---|
| `systemd/surgical-cv-worker.service` | One-shot unit invoking `python -m app.worker --once`. |
| `systemd/surgical-cv-worker.timer` | Fires the service every 5 minutes (OnUnitActiveSec=5min, AccuracySec=30s, Persistent=true). |

## Worker install (user-scoped)

User-scoped systemd is the recommended path on the DGX — the worker reads
the user's NAS mounts and the user's venv. No root needed.

```bash
# 1. Stage the unit files into the per-user systemd dir.
mkdir -p ~/.config/systemd/user
cp deploy/systemd/surgical-cv-worker.service ~/.config/systemd/user/
cp deploy/systemd/surgical-cv-worker.timer ~/.config/systemd/user/

# 2. Drop in any host-specific environment (paths, etc.).
#    Example: if PIPELINE_NAS_ROOT isn't /mnt/nas on this host:
mkdir -p ~/.config/systemd/user/surgical-cv-worker.service.d
cat > ~/.config/systemd/user/surgical-cv-worker.service.d/override.conf <<'EOF'
[Service]
Environment=PIPELINE_NAS_ROOT=/mnt/nas
Environment=APP_DB_PATH=%h/projects/surgical-cv/app/db/app.db
EOF

# 3. Reload + enable + start the timer.
systemctl --user daemon-reload
systemctl --user enable --now surgical-cv-worker.timer

# 4. Linger so the timer fires while the user is logged out.
sudo loginctl enable-linger "$USER"

# 5. Verify.
systemctl --user list-timers surgical-cv-worker.timer
journalctl --user -u surgical-cv-worker.service -f
```

## Worker install (system-scoped)

If the worker needs to run as a system service (e.g., NAS mounts only
visible to root), copy the units to `/etc/systemd/system/` and remove the
`%h` substitutions in `WorkingDirectory` / `ExecStart`. Set `User=` and
`Group=` to the unprivileged account that owns the venv.

## Operator commands

```bash
# Run a single iteration manually (bypasses the timer schedule).
systemctl --user start surgical-cv-worker.service
journalctl --user -u surgical-cv-worker.service -n 200 --no-pager

# Pause / resume the timer.
systemctl --user stop surgical-cv-worker.timer
systemctl --user start surgical-cv-worker.timer

# Re-trigger a quarantined marker: move it from .failed/ back to the
# raw-<surgeon>/ folder; the worker re-picks on next iteration.
mv /mnt/nas/raw-sarin/.failed/.ready-UCD-FIL-042.json /mnt/nas/raw-sarin/
```

## Pre-install verification

Before enabling on a new host, sanity-check the unit syntax with
`systemd-analyze`:

```bash
systemd-analyze --user verify deploy/systemd/surgical-cv-worker.service
systemd-analyze --user verify deploy/systemd/surgical-cv-worker.timer
```

(Non-zero exit = syntax issue. The verifier can't validate `%h` expansion
without the unit installed; copy first, then verify in place.)
