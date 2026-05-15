# Audit Deferrals

Findings from `docs/audits/codebase_audit_2026-05-15.md` that the bucket-2
cleanup pass (commits `873c4c0` → `087c0b7`) explicitly accepted rather than
fixed. Each is documented here so future contributors and reviewers know
the rationale and conditions for revisit.

| ID | Item | Disposition |
|---|---|---|
| F-019 | `SurgeonScope` / `AdminScope` abstract stubs that raise `NotImplementedError` (`read_case`, `write_case_metadata`, `trigger_pipeline`, `resolve_audit_flag`, `reupload_metadata`) | Folded into per-tab specs — each method gets its body when the corresponding surgeon / admin tab is built. Stubs serve as a schema contract, not active dead code. |
| F-026 | `bench/test_cases.json` couples surgeon names to specific `UCD-FIL-###` IDs | Accepted. No patient PHI; case IDs are fabricated. The only concern was a quasi-linking record in committed code — informational risk only. Surfaced to PI; no action required. |
| F-028 | `pytest --cov` reports ~74% but real coverage is ~12 points higher | Headline number is misleading. `app/db/admin_cli.py`, `app/db/init_db.py`, `pipeline/cli.py`, both `__main__` modules, and ~64% of `pipeline/commands/metadata.py` are exercised exclusively via subprocess (`subprocess.run([sys.executable, "-m", ...])` in `tests/test_admin_cli.py` and `tests/test_cli_metadata.py`), which `pytest-cov` does not instrument by default. Re-running with `coverage run --parallel-mode` + `[coverage:run] concurrency = multiprocessing` + a `sitecustomize.py` would produce a faithful number; not worth the operational complexity for a v1 system. Headline coverage stays understated by design. |
| F-029 | `fetch_picklists` makes 4 sequential SQLite roundtrips per surgeon page load | Accepted at v1. ~4 ms total on local SQLite — tolerable. Revisit when (a) page-load latency becomes a complaint or (b) the picklist set crosses ~10 fields. |
