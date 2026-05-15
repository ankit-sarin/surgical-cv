"""Shared helpers across pipeline.commands.* — kept private (leading
underscore on the module name) so external callers go through the
subcommand entry points, not the helpers."""

from __future__ import annotations


def format_cli_error(case_id: str, error_summary: str) -> str:
    """F-033: standardize the per-case stderr line shape across concat /
    deid / verify. Operator-facing (not surgeon-facing — these messages
    print to stderr during CLI invocations), so the path-disclosure
    threat model is much smaller than the surgeon-UI surfaces. The
    consolidation is here for shape consistency, not security."""
    return f"  {case_id}: FAILED — {error_summary}"
