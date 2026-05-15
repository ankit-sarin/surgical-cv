# Codebase audit — 2026-05-15

## Run metadata

| Field | Value |
|---|---|
| Audit date (UTC) | 2026-05-15T09:14:02Z |
| Branch | main |
| Commit SHA | 3323837c346aba1b7fe594fc4eff5b11bde642e9 |
| Last commit | feat: Q3 decoupled worker — bridges Intake submit to pipeline CLI |
| Test count | 757 passed in 52.85s |
| Coverage (overall) | 74% (2127/2893 stmts covered) — but this **understates real coverage**. The admin CLI (689 lines), `init_db`, `pipeline.cli`, both `__main__` modules, and ~64% of `pipeline/commands/metadata.py` are exercised exclusively by subprocess-based tests, which `pytest-cov` does not instrument. See F-028. |
| Scope walked | `app/`, `pipeline/`, `tests/`, `scripts/`, `deploy/` |
| Skipped | `.venv/`, `__pycache__/`, `app/db/app.db`, `/mnt/nas/*`, `bench/` (out of scope per task spec; one informational finding still surfaced) |
| Subagents | `phi-checker` (8 findings), `code-reviewer` (12 findings), 9 primary findings; deduped + IDed by primary |

## Summary

| Category | P0 | P1 | P2 | Total |
|---|---:|---:|---:|---:|
| Redundancies | 0 | 0 | 4 | 4 |
| Architectural inconsistencies | 0 | 2 | 1 | 3 |
| Dead code | 0 | 0 | 1 | 1 |
| Tech debt | (folded into Redundancies) | | | |
| Security / PHI | 2 | 9 | 8 | 19 |
| Test coverage | 0 | 0 | 1 | 1 |
| Performance | 0 | 0 | 1 | 1 |
| **Total** | **2** | **11** | **16** | **29** |

Two P0s — both timeout gaps — are the hard blockers for the UCD-FIL-005 smoke. Without them, a slow Ollama load or a pathological video file silently consumes the worker's full 30-minute systemd `TimeoutStartSec` while every other surgeon's pending markers wait behind the single-worker lock.

## Triage shortlist (P0 + P1, in ID order)

1. **F-001** · `ollama.chat()` has no timeout — worker can block indefinitely on a cold qwen3:32b load
2. **F-002** · `ffprobe` / `exiftool` / null-mux subprocesses in `collect_evidence` have no timeout
3. **F-003** · `metadata --edit notes` writes the full free-text value verbatim to `pipeline.log` on the NAS
4. **F-004** · `metadata --show` prints `Notes: <free text>` to journalctl-captured stdout
5. **F-005** · `app/phi.py` does not detect names, addresses, or separator-formatted phone numbers
6. **F-006** · Worker writes the first 1 KB of subprocess stderr into `attention_items.details` without scrubbing
7. **F-007** · `scripts/migrate_manifest_spec_j.py` prints the first two manifest rows (incl. `notes`) to stdout on every run
8. **F-008** · Partial-auth token carries the DSM password in base64-decoded plaintext for 120 s
9. **F-009** · `APP_SESSION_SECRET` is accepted at any length (one character passes)
10. **F-010** · `app/worker/failures.py` leaks SQLite connections per iteration (daemon mode only)
11. **F-011** · `SubmitError` reflects raw filesystem exception strings (incl. NAS paths) into the surgeon UI
12. **F-012** · `PIPELINE_NAS_ROOT` and `RAW_VIDEO_ROOT` are two env vars for the same conceptual root — silent drift puts markers and the worker scan in different folders
13. **F-013** · `CsvCaseRepository.submit_case` trusts `partial_row["surgeon"]` from its caller without re-checking against the authenticated scope

---

## Findings

### F-001 · ollama.chat() has no timeout — worker can block indefinitely

- **Severity:** P0
- **Category:** Security / Performance
- **Location:** `pipeline/diagnostician.py:219-234`
- **Evidence:**
  ```python
  response = ollama.chat(
      model=OLLAMA_MODEL,
      messages=[{"role": "user", "content": prompt}],
      format="json",
      options={"temperature": OLLAMA_TEMPERATURE,
               "num_predict": OLLAMA_NUM_PREDICT},
      think=False,
  )
  ```
  No `timeout` kwarg; the underlying `ollama.Client()` is constructed with `Timeout(timeout=None)`.
- **Why it matters:** qwen3:32b on the DGX Blackwell can take up to 300 s per call (per global CLAUDE.md). On a cold model load or under GPU pressure, a single call can consume the worker's full 30-minute `TimeoutStartSec` from `deploy/systemd/surgical-cv-worker.service`. During that window the single-worker `fcntl.flock` blocks every other pending case across all surgeons. The UCD-FIL-005 smoke is the first call that will hit a cold qwen3:32b in this codepath.
- **Suggested disposition:** Fix before live smoke.

### F-002 · ffprobe / exiftool / null-mux subprocesses have no timeout

- **Severity:** P0
- **Category:** Security / Performance
- **Location:** `pipeline/diagnostician.py:58-108`
- **Evidence:**
  ```python
  ffprobe_result = subprocess.run([...], capture_output=True, text=True)
  ...
  null_mux = subprocess.run([...], capture_output=True, text=True)
  ```
  Three subprocess calls in `collect_evidence`; none specify `timeout=`.
- **Why it matters:** A corrupt video, a stalled NFS read, or an exotic ffprobe condition can hang any of these calls indefinitely. Same blast radius as F-001 — the lock is held, every other case waits. Add `timeout=60` for ffprobe/exiftool and `timeout=600` for the null-mux decode pass; catch `subprocess.TimeoutExpired` and convert to a `RuntimeError` so the verify command's existing exception handler treats it as a failure rather than a hang.
- **Suggested disposition:** Fix before live smoke.

### F-003 · metadata --edit writes notes value verbatim to pipeline.log

- **Severity:** P1
- **Category:** Security / PHI
- **Location:** `pipeline/commands/metadata.py:120-122` (audit_args), `393-404` (validation-failure audit), `441-453` (success audit)
- **Evidence:**
  ```python
  def _audit_args(args: Namespace) -> dict:
      field, value = args.edit
      return {"edit_field": field, "edit_value": value, "confirm": True}
  ...
  log_audit(..., "success", case=case_id, details={
      "field": field,
      "before": before_value,
      "after": coerced,         # for notes: full free-text string
  })
  ```
- **Why it matters:** Every confirmed `metadata --edit notes "..."` writes the full surgeon-typed string into `pipeline.log` on the NAS — three slots: `args.edit_value`, `details.before`, `details.after`. The audit log is operator-readable, append-only, and not gitignored. UCD-FIL-005 will not run a metadata edit, so this does not block the smoke; the moment a surgeon needs to correct a note, it does. `or_room` exposes the same vector at lower risk.
- **Suggested disposition:** Fix before surgeon onboarding. One-line guard: when `field == "notes"`, replace `edit_value` / `before` / `after` with `"<redacted>"` (keep `field` so the audit still shows what was edited).

### F-004 · metadata --show prints notes to journalctl-captured stdout

- **Severity:** P1
- **Category:** Security / PHI
- **Location:** `pipeline/commands/metadata.py:504-526`
- **Evidence:**
  ```python
  rows: list[tuple[str, str]] = [
      ...,
      ("Notes:", manifest_row.notes if manifest_row.notes else "(empty)"),
  ]
  lines = [f"{label:<{_LABEL_WIDTH}}{value}" for label, value in rows]
  ```
- **Why it matters:** Operators who run `python -m pipeline metadata UCD-FIL-005` from a systemd-managed shell or via any wrapper that captures stdout (e.g., a future "show case" admin tab calling out to the CLI) will land the full notes text in journalctl / log captures. The read path correctly skips the audit log; the leak is via the terminal channel.
- **Suggested disposition:** Fix before surgeon onboarding. Either gate `notes` behind a `--show-notes` flag or render `Notes: (length=NN)` with the body truncated/redacted.

### F-005 · app/phi.py doesn't catch names, addresses, or formatted phones

- **Severity:** P1
- **Category:** Security / PHI
- **Location:** `app/phi.py:26-46`
- **Evidence:**
  ```python
  _MRN_PATTERN = re.compile(r"(?<!\d)\d{7,10}(?!\d)")
  _SSN_PATTERN = re.compile(r"(?<!\d)\d{3}[-\s]\d{2}[-\s]\d{4}(?!\d)")
  _DATE_PATTERN = re.compile(r"(?<!\d)\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}(?!\d)")
  # No pattern for names, addresses, or (916) 555-1234 style phones.
  ```
- **Why it matters:** The module's own docstring labels itself "the single source of truth for what counts as a PHI category" and notes that the post-submit gemma4:26b scrubber will reuse the categories. A note like `"Dr. Chen referred from Kaiser, patient John Doe"` passes the soft-warning gate cleanly — no `phi_confirm` dialog appears, the surgeon submits, and the unscrubbed text lands in `case_manifest.csv`. False positives are tolerable; false negatives on the most common identifier (a name) are not.
- **Suggested disposition:** Fix before surgeon onboarding. Add at minimum a name heuristic (e.g., consecutive title-case tokens after `Dr.` / `Pt.` / `Patient:`) and a separator-formatted phone pattern.

### F-006 · Worker writes 1 KB of subprocess stderr to attention_items.details unscrubbed

- **Severity:** P1
- **Category:** Security / PHI
- **Location:** `app/worker/dispatch.py:33`, `180-185`, `210-213`, `247-249`; `app/worker/failures.py:159-170`
- **Evidence:**
  ```python
  _STDERR_TRUNC = 1024
  ...
  return DispatchOutcome(
      kind="hard_fail", stage="concat",
      returncode=rc.returncode,
      detail=_truncate(rc.stderr),
  )
  ```
  And in `failures.py`:
  ```python
  details=(f"pipeline {outcome.stage} stage failed "
           f"(returncode={outcome.returncode}): {outcome.detail}"),
  ```
- **Why it matters:** Pipeline subprocess stderr can carry full NAS paths and (at the deid stage) `verification_notes` strings that may have been written by a previous run. `attention_items.details` is read by the Action Required tab a surgeon will see. Today's exception messages don't include manifest free-text, so the path is latent rather than active — but no guard prevents that.
- **Suggested disposition:** Fix before surgeon onboarding. Either truncate to a known-safe summary (stage + returncode + first-line-only) or scrub the stderr block through `app/phi.py` before persistence.

### F-007 · migrate_manifest_spec_j.py prints first two rows to stdout on every run

- **Severity:** P1
- **Category:** Security / PHI
- **Location:** `scripts/migrate_manifest_spec_j.py:143-147`
- **Evidence:**
  ```python
  print("preview (first 2):")
  for r in rows_new[:2]:
      print("  " + ",".join(r.values()))
  ```
  `r.values()` includes `notes` and `or_room`.
- **Why it matters:** The migration ran once on 2026-05-15 (per primer.md) but the script remains on disk and may be invoked again on a populated manifest if a future schema change forks from this template. Both `--dry-run` and `--commit` paths print the first two rows verbatim — including any free-text fields. The script is gitignored (`scripts/`) so this never lands in commit history, but the journalctl capture on the operator host is the leak surface.
- **Suggested disposition:** Cleanup pass. Trim the preview to schema columns + counts, not row values.

### F-008 · Partial-auth token carries DSM password in base64 plaintext

- **Severity:** P1
- **Category:** Security
- **Location:** `app/auth.py:79-83`, `102-103`; `app/main.py:81` (HTML hidden input)
- **Evidence:**
  ```python
  def encode_partial_auth(username: str, password: str) -> str:
      return _partial_serializer().dumps({"u": username, "p": password})
  ```
  `URLSafeTimedSerializer` signs (HMAC) but does not encrypt. A live token decoded via base64url to `b'{"u":"user","p":"supersecretpassword123"}'`. Token sits in `<input type="hidden">` for up to 120 s.
- **Why it matters:** Any browser devtools session, MITM position on the Cloudflare tunnel, or HTTPS proxy log capture during the OTP window reads the DSM account password in plaintext. The HMAC protects integrity (no forgery) but not confidentiality. Threat model is private network + DSM auth; exposure window is short, but the credential is the surgeon's NAS account password — high blast radius if leaked.
- **Suggested disposition:** Fix before surgeon onboarding. Either (a) keep only the username in the token and stash the password in a server-side dict keyed by a random nonce, or (b) wrap the payload with `cryptography.fernet.Fernet` using a key derived from `APP_SESSION_SECRET`.

### F-009 · APP_SESSION_SECRET has no minimum-length enforcement

- **Severity:** P1
- **Category:** Security
- **Location:** `app/auth.py:73-76`, `80-83`
- **Evidence:**
  ```python
  secret = os.environ.get("APP_SESSION_SECRET")
  if not secret:
      raise RuntimeError("APP_SESSION_SECRET env var is required")
  return URLSafeTimedSerializer(secret, salt="session")
  ```
  Truthiness check only; no length validation.
- **Why it matters:** itsdangerous uses HMAC-SHA1, whose security depends on key entropy. A short secret is brute-forceable offline against any captured cookie. The primer.md documents "≥32 bytes" as a requirement; the code does not enforce it. An operator setting `APP_SESSION_SECRET=dev` (a real risk in a hand-typed `.env` file) is silently accepted.
- **Suggested disposition:** Fix before surgeon onboarding. Add `if len(secret) < 32: raise RuntimeError("APP_SESSION_SECRET must be ≥32 bytes")` once, in a shared helper called by both serializer constructors.

### F-010 · DB connection leak in app/worker/failures.py daemon mode

- **Severity:** P1
- **Category:** Security / Reliability
- **Location:** `app/worker/failures.py:38-50`, `57-65`, `101-118`
- **Evidence:**
  ```python
  with connect() as conn:
      conn.execute(...)
      conn.commit()
  ```
  `sqlite3.Connection.__exit__` commits/rollbacks but does NOT close the connection — `app/auth.py:181-202` and `app/main.py:113-124` correctly use `try/finally: conn.close()` instead.
- **Why it matters:** Production runs `--once` per timer fire (process exits, FDs released — no leak in the production happy path). `--daemon` mode is documented for "local dev and operator-side smokes" — the live UCD-FIL-005 smoke may use it. Each iteration leaks at minimum 1 connection (`ensure_system_worker_user`) plus 2 per dispatched marker (`_lookup_username_for_slug` + `write_attention_item`). At 5-minute scan intervals this exhausts FDs in hours.
- **Suggested disposition:** Fix before live smoke if the smoke uses `--daemon`; otherwise fix before surgeon onboarding. Wrap each `connect()` in `try/finally: conn.close()`.

### F-011 · SubmitError surfaces NAS filesystem path to the surgeon UI

- **Severity:** P1
- **Category:** Security / UX
- **Location:** `app/repos/cases.py:240`, `app/surgeon_app.py:771-773`
- **Evidence:**
  ```python
  # repos/cases.py:240
  raise SubmitError(f"manifest write failed: {e}") from e
  ```
  ```python
  # surgeon_app.py:771
  outputs[1] = gr.update(value=(
      f"⚠️ Submission failed: {outcome.infra_error}"
  ))
  ```
- **Why it matters:** A `PermissionError` from the CSV write reflects the full NAS path back to the browser — `[Errno 13] Permission denied: '/mnt/nas/or-raw/case_manifest.csv'`. Path disclosure to a non-admin user, plus the surgeon now knows there's a problem they cannot diagnose. UCD-FIL-005 won't fire this (the smoke environment has correct perms), but the first time NAS permissions hiccup in production the surgeon sees infrastructure.
- **Suggested disposition:** Fix before surgeon onboarding. Log the full exception server-side, raise `SubmitError("manifest unavailable — contact your coordinator")` with a generic message.

### F-012 · Two NAS-root env vars (PIPELINE_NAS_ROOT vs RAW_VIDEO_ROOT) can drift

- **Severity:** P1
- **Category:** Architectural inconsistency
- **Location:** `pipeline/paths.py:21-33` vs `app/repos/segments.py:31-35` and `app/repos/cases.py` (uses `raw_root()` from `segments.py`)
- **Evidence:**
  ```python
  # pipeline/paths.py
  env_root = os.environ.get("PIPELINE_NAS_ROOT")
  root = env_root if env_root else "/mnt/nas"
  ```
  ```python
  # app/repos/segments.py
  env = os.environ.get("RAW_VIDEO_ROOT")
  if env: return Path(env)
  return _DEFAULT_RAW_ROOT  # Path("/mnt/nas")
  ```
  The marker writer (`app/repos/cases.py:_write_ready_marker`) uses `RAW_VIDEO_ROOT`. The worker scanner (`app/worker/main.py:142`) uses `PIPELINE_NAS_ROOT`.
- **Why it matters:** Defaults agree (`/mnt/nas`), so the production happy path works. But if an operator sets only one (say `PIPELINE_NAS_ROOT=/srv/nas` for the worker systemd drop-in but the FastAPI service still inherits `RAW_VIDEO_ROOT` unset, defaulting to `/mnt/nas`), markers are written to one location and the worker scans another — silently. Markers vanish into the gap, the surgeon sees "Submitted" and nothing happens. This is the classic "two configs for the same thing" failure mode.
- **Suggested disposition:** Fix before surgeon onboarding (or before any non-default deployment). Either reduce to a single env var (preferred: `PIPELINE_NAS_ROOT`) or have one read the other as fallback. `deploy/README.md` already only mentions `PIPELINE_NAS_ROOT` — the divergence is invisible to operators following the install steps.

### F-013 · submit_case trusts caller-supplied surgeon — bypass risk

- **Severity:** P1
- **Category:** Architectural inconsistency
- **Location:** `app/repos/cases.py:206-257`, called from `app/surgeon_app.py:734-743`
- **Evidence:**
  ```python
  # surgeon_app.py — correct: surgeon comes from authenticated scope
  outcome: SubmitOutcome = handle_submit_request(
      surgeon=scope.folder_slug, ...
      submit_fn=scope.repos.case.submit_case,
  )
  ```
  ```python
  # cases.py:206-238 — submit_case trusts the partial_row dict
  row = CaseManifestRow(
      ucd_fil_id=new_id,
      surgeon=partial_row["surgeon"],
      ...
  )
  ```
  No assertion that `partial_row["surgeon"]` matches an authenticated identity.
- **Why it matters:** Today's only caller (`surgeon_app._dispatch_submit`) does the right thing, threading `scope.folder_slug` through. The repo method itself takes any `surgeon` value the caller supplies. The next caller — admin-side cross-surgeon submission, the future "Action Required" tab, a CLI helper — could pass a surgeon string from anywhere. The marker filename uses `partial_row["surgeon"]` to pick the destination folder, so a bad surgeon arg writes a marker into the wrong folder, which the worker then dispatches as if owned by that folder. Defense-in-depth gap, not an active vulnerability.
- **Suggested disposition:** Fix before surgeon onboarding. Pass `expected_surgeon: str` to `submit_case` and assert equality, or invert the API so the repo holds the authenticated surgeon at construction.

### F-014 · Atomic write pattern duplicated three times

- **Severity:** P2
- **Category:** Redundancies
- **Location:** `pipeline/csv_io.py:149-172` (`CsvTable._commit`), `app/repos/cases.py:71-108` (`_write_ready_marker`), `scripts/migrate_manifest_spec_j.py:101-119` (`_atomic_write`)
- **Evidence:** All three implement the same `mkstemp(dir=parent) → write → fsync → os.replace → cleanup-on-exception` pattern with subtle differences (different prefix conventions, different error swallowing, different `unlink-on-fail` blocks).
- **Why it matters:** Three implementations means three places where a future hardening (e.g., adding `os.fchmod` for stricter perms, or directory `fsync` for true POSIX-strict atomicity) needs to land. Today's behavior is consistent; tomorrow's might not be.
- **Suggested disposition:** Cleanup pass. Extract to `pipeline/atomic_write.py::write_atomic(path, body_writer)`; have all three callers use it.

### F-015 · BDV regex duplicated in segments.py and ffmpeg.py with different rules

- **Severity:** P2
- **Category:** Redundancies
- **Location:** `app/repos/segments.py:28` vs `pipeline/ffmpeg.py:8`
- **Evidence:**
  ```python
  # segments.py — rejects -copied
  _BDV_CANONICAL_RE = re.compile(r"^capt0_(\d{8})-(\d{6})\.mp4$")
  # ffmpeg.py — accepts both
  _BDV_FILENAME_RE = re.compile(r"^capt0_(\d{8}-\d{6})(?:-copied)?\.mp4$")
  ```
- **Why it matters:** The semantic split (intake view excludes claimed segments; concat helper accepts both) is correct, but the two patterns must stay in sync any time the BDV recorder changes its filename convention. There is no shared source of truth.
- **Suggested disposition:** Cleanup pass. Move both into `pipeline/bdv.py` with named constants `BDV_UNCLAIMED_RE` and `BDV_ANY_RE`.

### F-016 · UCD-FIL-### regex duplicated across many files

- **Severity:** P2
- **Category:** Redundancies
- **Location:** at least `app/repos/cases.py:42`, `app/worker/scan.py:27`, `pipeline/schemas.py:61`, `pipeline/commands/metadata.py:43`, `pipeline/commands/deid.py:21`, `pipeline/commands/verify.py:46-47`
- **Evidence:** Variants include `^UCD-FIL-(\d{3,})$` (cases.py — accepts ≥3 digits!), `^UCD-FIL-\d{3}$` (others), `^UCD-FIL-\d{3}_video\.mp4$` (verify PF3). The "≥3 digits" form lets `_next_ucd_fil_id` survive a future case 1000+, but every other consumer's `\d{3}` would reject it.
- **Why it matters:** First inconsistency that will fire is the 1000th case (UCD-FIL-1000) — `submit_case` allocates it via the lenient regex, but `CaseManifestRow.ucd_fil_id` rejects it (`pattern=r"^UCD-FIL-\d{3}$"`), the transaction raises, and `submit_case` fails. The corpus is currently four cases, so the immediate risk is zero; the alignment debt is real.
- **Suggested disposition:** Cleanup pass. Define `CASE_ID_RE_STR = r"^UCD-FIL-\d{3}$"` once in `pipeline/schemas.py`, import everywhere.

### F-017 · `_SURGEON_RE` and `_VERIFICATION_NOTES_MAX` duplicated across pipeline commands

- **Severity:** P2
- **Category:** Redundancies
- **Location:** `pipeline/commands/concat.py:25-26`, `pipeline/commands/deid.py:20-22`, `pipeline/commands/verify.py:45,50`
- **Evidence:** All three files define `_SURGEON_RE = re.compile(r"^[a-z][a-z0-9-]*$")` and `_VERIFICATION_NOTES_MAX = 200` independently.
- **Why it matters:** Same drift hazard as F-015/F-016 with very small surface area. Cleanup-only.
- **Suggested disposition:** Cleanup pass. Hoist into `pipeline/schemas.py` or a new `pipeline/constants.py`.

### F-018 · scripts/ holds three pieces of dead/done code

- **Severity:** P2
- **Category:** Dead code
- **Location:** `scripts/seed_intake.py`, `scripts/migrate_manifest_spec_j.py`, `scripts/manifest_pre_13E_corrections.csv`
- **Evidence:**
  - `seed_intake.py:66` instantiates `CaseManifestRow(... procedure_name="Other", ...)` — but Spec J retired `procedure_name` (see `tests/test_schemas.py:59`: `assert "procedure_name" not in CASE_MANIFEST_COLUMNS`). The script would crash on first append.
  - `migrate_manifest_spec_j.py` already ran once on 2026-05-15 (primer.md item #11). Backup exists at `case_manifest.csv.pre-spec-j.20260515T080425Z.bak`.
  - `manifest_pre_13E_corrections.csv` carries the pre-Spec-J 8-column header — unreferenced by any code.
  
  All three are gitignored (`.gitignore:9` contains `scripts/`), so none are tracked by git.
- **Why it matters:** None affect production. The risk is operator confusion: someone runs `python scripts/seed_intake.py` thinking it bootstraps a new surgeon, hits the field-name crash, doubts the rest of the pipeline.
- **Suggested disposition:** Cleanup pass. Delete all three locally (they're already not in version control). The active migration is documented; the seed flow is replaced by Section 5 submit.

### F-019 · SurgeonScope/AdminScope methods are abstract stubs that raise NotImplementedError

- **Severity:** P2
- **Category:** Architectural inconsistency
- **Location:** `app/scopes.py:136-219`
- **Evidence:** `read_case`, `write_case_metadata`, `trigger_pipeline`, `resolve_audit_flag`, `reupload_metadata` — five methods on each scope — all raise `NotImplementedError` (or `ScopeViolationError` for the admin-only ones on the surgeon side). No production code path calls them today.
- **Why it matters:** Not active dead code (the abstract base shape is load-bearing for type checking and future specs), but the stubs are accumulating. Each spec that adds a tab is supposed to fill one in; until then they're trip wires for any introspection (e.g., a future `dir(scope)` based UI generator) that calls them blindly.
- **Suggested disposition:** Document and accept. Fold into the per-tab specs that wire each method.

### F-020 · DSM verify=False is hardcoded, not env-gated

- **Severity:** P2
- **Category:** Security
- **Location:** `app/auth.py:145`
- **Evidence:**
  ```python
  resp = httpx.post(url, data=params, verify=False, timeout=10.0)
  ```
- **Why it matters:** The docstring acknowledges this is acceptable for the private-network / self-signed-cert link (lines 36-38). Fine for the smoke. Worth env-gating before any non-dev deployment.
- **Suggested disposition:** Cleanup pass. `verify=os.environ.get("DSM_TLS_VERIFY", "0") != "0"` (preserves current default; flips with one env var).

### F-021 · app.db is mode 644 (world-readable on the DGX)

- **Severity:** P2
- **Category:** Security
- **Location:** `/home/ankitsarin/projects/surgical-cv/app/db/app.db` (current mode 644); created by `app/db/init_db.py` at default sqlite3 perms
- **Evidence:** `stat` reports mode 644. The DB holds usernames, roles, folder_slugs, display_names, emails, last_login timestamps. `*.db` is gitignored — it has never been committed (verified via `git log` and `git ls-files | grep db`).
- **Why it matters:** Any local user on the DGX can read the surgeon roster + email addresses without authenticating to the FastAPI app. Not patient PHI, but operational user metadata that should be owner-only.
- **Suggested disposition:** Cleanup pass. `chmod 600` immediately; in `init_db.py`, set `os.umask(0o077)` before `sqlite3.connect()` so future re-creations are 0o600.

### F-022 · dispatch_marker uses batch concat, asymmetric with deid/verify

- **Severity:** P2
- **Category:** Security / Reliability
- **Location:** `app/worker/dispatch.py:57-58`, `178`
- **Evidence:**
  ```python
  def concat(self, surgeon: str) -> SubprocessResult:
      return self._run(["concat", "--surgeon", surgeon])
  ...
  rc = driver.concat(marker.surgeon)  # not --case
  ```
  `deid` and `verify` are per-case; only `concat` is batch.
- **Why it matters:** When two `sarin` markers are processed in the same iteration, the first dispatch runs `concat --surgeon sarin` which advances both intake rows; the second dispatch's post-concat check (`state.stage == Stage.intake`, line 187) silently passes because the state is already `concatenated`. Functionally OK today. The asymmetry hurts when concat fails — the transaction rolls back all in-flight concats for that surgeon, so one bad case fails its sibling too. Add `--case` to `concat` (already exists for deid/verify) for symmetry.
- **Suggested disposition:** Cleanup pass.

### F-023 · No server-side validation that submitted segment filenames belong to the surgeon

- **Severity:** P2
- **Category:** Security
- **Location:** `app/intake/submit.py:48-58`, `app/repos/cases.py:206-257`, `pipeline/schemas.py:140-148`
- **Evidence:** `ValidationContext.segments_selected` is a list[str] of filenames from Gradio client state. The validator only checks non-empty. `PipelineStateRow._segments_pipe_safe` only blocks `|`. No regex check against `_BDV_CANONICAL_RE`, no `is_relative_to` guard before constructing `raw_dir / seg` in concat.
- **Why it matters:** Gradio state is server-side, so a normal user can't forge the segment list. Session compromise + a crafted submission could plant `segments: ["../other-surgeon/secret.mp4"]`. ffprobe / ffmpeg would then operate on files outside the surgeon's folder. Marker write itself is constrained (the marker payload's `segments` is validated only for type, not pattern, in `app/worker/scan.py:115-121`).
- **Suggested disposition:** Cleanup pass. In `submit_case` (or in `validate_submission`), require each filename to match `^capt0_\d{8}-\d{6}(?:-copied)?\.mp4$` and that `(raw_root / f"raw-{surgeon}" / name).resolve().is_relative_to(raw_root)`.

### F-024 · ffmpeg concat-list path escape doesn't guard newlines

- **Severity:** P2
- **Category:** Security
- **Location:** `pipeline/ffmpeg.py:115-116`, `178-181`
- **Evidence:**
  ```python
  def _escape_concat_path(p: str) -> str:
      return p.replace("'", "'\\''")
  ...
  f.write(f"file '{escaped}'\n")
  ```
- **Why it matters:** `subprocess.run` doesn't use a shell, so there's no shell-injection vector. ffmpeg's concat demuxer parses the list line-by-line, so a newline in a path corrupts the format (next line interpreted as a directive). NAS paths from BDV recordings shouldn't contain newlines. Defensive only.
- **Suggested disposition:** Cleanup pass. Add `if "\n" in p: raise ValueError(...)` to `_escape_concat_path`.

### F-025 · metadata --confirm exception writes full Python traceback to pipeline.log

- **Severity:** P2
- **Category:** Security
- **Location:** `pipeline/commands/metadata.py:432`
- **Evidence:**
  ```python
  details={
      "failure_kind": "exception",
      "error": traceback.format_exc(),
  },
  ```
- **Why it matters:** `traceback.format_exc()` includes file paths from the surgical-cv source tree (`/home/ankitsarin/projects/surgical-cv/pipeline/csv_io.py:...`). Operator log only — not surgeon-facing — but inconsistent with concat/deid/verify which use `str(e)`.
- **Suggested disposition:** Cleanup pass. Replace with `str(e)` + `type(e).__name__` to match the rest of the pipeline.

### F-026 · bench/test_cases.json couples surgeon names with specific UCD-FIL IDs in committed code

- **Severity:** P2
- **Category:** Security / PHI (informational — out of explicit audit scope)
- **Location:** `bench/test_cases.json` (entire file)
- **Evidence:** The PHI subagent surfaced this despite `bench/` being out of explicit scope. The file is committed; it pairs `/mnt/nas/raw-sarin/UCD-FIL-001_raw.mp4`, `/mnt/nas/deid-miller/UCD-FIL-...` etc. across all five surgeons in synthetic ffprobe/ffmpeg output blocks.
- **Why it matters:** No patient PHI. The case IDs are fabricated examples. The only concern is that this is the one place in committed code where a surgeon name is durably associated with a specific UCD-FIL-### ID — a quasi-linking record if anyone ever reverse-mapped via REDCap. Almost certainly fine; flagged for awareness, not action.
- **Suggested disposition:** Document and accept (out of scope for this audit; surface to PI for confirmation).

### F-027 · .gitignore doesn't cover *.bak migration backups

- **Severity:** P2
- **Category:** Security / PHI
- **Location:** `.gitignore`
- **Evidence:** Covers `venv/`, `__pycache__/`, `.env`, `*.db`, `data/`, `scripts/`. Does NOT cover `*.bak`. Migration backups (e.g., `case_manifest.csv.pre-spec-j.20260515T080425Z.bak`) currently live on the NAS, not in the repo, so this is a latent gap.
- **Why it matters:** If a future migration writes `.bak` into the repo tree (e.g., for an `app/db/`-internal schema migration), that backup would be eligible for `git add` without an explicit `*.bak` block.
- **Suggested disposition:** Cleanup pass. Add `*.bak` to `.gitignore`.

### F-028 · pytest-cov understates real coverage by ~12 percentage points

- **Severity:** P2
- **Category:** Test coverage
- **Location:** `tests/test_admin_cli.py:21-30`, `tests/test_cli_metadata.py`, `tests/test_cli_help.py`
- **Evidence:** Coverage report:
  ```
  app/db/admin_cli.py               362    362     0%   21-689
  app/db/init_db.py                  28     28     0%   11-58
  pipeline/cli.py                    47     47     0%   1-97
  pipeline/commands/metadata.py     213    136    36%   ...
  ```
  All four are tested by `subprocess.run([sys.executable, "-m", ...])` invocations (e.g. `tests/test_admin_cli.py:21-30`, `tests/test_cli_metadata.py` has 40 such tests). pytest-cov does not instrument child Python processes by default.
- **Why it matters:** The headline 74% number suggests larger gaps than exist. Real coverage of the listed modules is high (50 admin-CLI tests, 40 metadata tests). The audit report flags this so future "improve coverage" work doesn't waste effort retesting already-tested code.
- **Suggested disposition:** Cleanup pass. Add `coverage run --parallel-mode` + a `[coverage:run] concurrency = multiprocessing` section in `.coveragerc`, and a `sitecustomize.py` that calls `coverage.process_startup()`. Or accept the gap and document it in primer.md / README.

### F-029 · fetch_picklists makes 4 sequential SQLite roundtrips per page load

- **Severity:** P2
- **Category:** Performance
- **Location:** `app/surgeon_app.py:130-143`
- **Evidence:**
  ```python
  return {
      field: scope.repos.picklist.list_active(field, scope.specialty)
      for field in _PICKLIST_FIELDS_FOR_INTAKE
  }
  ```
  Each `list_active` opens a new sqlite3 connection (`SqlitePicklistRepository:49-70`), runs one SELECT, closes.
- **Why it matters:** ~four ms total on local SQLite — tolerable for v1. Will scale poorly when the admin app adds its own field set, or when picklist values grow with multi-specialty support. Module docstring already acknowledges no caching is intentional.
- **Suggested disposition:** Document and accept. Revisit when (a) page-load latency becomes a complaint or (b) the picklist set crosses ~10 fields.

---

## Negative findings (checked + clean)

To prevent future audits from re-checking the same things:

- `app/auth.py` cookie flags: `HttpOnly=True`, `Secure=True (unless dev mode)`, `SameSite=Lax`. ✓
- `CsvCaseRepository.submit_case` holds `LOCK_EX` across the read-max + append (no TOCTOU on `ucd_fil_id` allocation). ✓ (verified by `tests/test_submit_case_repo.py:331` concurrent-flock test)
- All three atomic-write sites (`csv_io._commit`, `_write_ready_marker`, `migrate_manifest_spec_j._atomic_write`) use same-directory tempfile + `fsync` + `os.replace`. ✓
- `.ready-*.json` payload contains only `ucd_fil_id`, `surgeon`, `submitted_at`, `segments` — no PHI fields. ✓
- All `subprocess.run` calls use argv lists; no `shell=True`; surgeon names validated against `^[a-z][a-z0-9-]*$` before reaching argv. ✓
- `csv.DictWriter` is the only CSV write path — no CSV-injection-via-string-concat vector. (Spreadsheet-formula values like `=SUM(...)` are stored as-is, which is correct CSV behavior; the injection risk is downstream consumers opening the file in Excel — operational policy, not a code bug.)
- `app/phi.py` returns counts only and never echoes matched text — verified by `tests/test_intake_section4.py:185-193`.
- `*.db` is gitignored; `app/db/app.db` has never been in git history.
- No `procedure_name` references survive in tracked code (only in gitignored `scripts/seed_intake.py` and a `not in CASE_MANIFEST_COLUMNS` negative assertion in `tests/test_schemas.py:59`).
- No `_seed_vocab`, `PIPELINE_VOCAB_DIR`, or `bench/vocabularies/procedures.json` references found in tracked code.
- `picklist` field convention (singular filenames, plural display labels) is consistent. ✓
- `conversion_target` three-state sentinel (`None` / `""` / value) is correctly encoded — `""` is rejected at validation time before reaching the on-disk row, where `""` means "no conversion".
- UserScope / ScopeViolationError enforcement is wired at the Gradio mount via `_gradio_auth_dep`; `SurgeonScope._require_case` guards every case-scoped method; admin-only methods on `SurgeonScope` unconditionally raise.

---

*End of report.*
