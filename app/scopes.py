"""UserScope hierarchy — the authoritative read/write surface for HTTP handlers.

Three classes:

- ``UserScope`` — abstract base declaring the full method surface from v18.
  Listing methods return a list; targeted methods take a case_id / flag_id.
- ``SurgeonScope(username, folder_slug)`` — listings filter by folder_slug;
  case-scoped methods raise ``ScopeViolationError`` for out-of-scope ids;
  admin-only methods (resolve_audit_flag, reupload_metadata) always raise
  ``ScopeViolationError``.
- ``AdminScope(username)`` — pass-through; listings see everything.

In Spec B the bodies are stubs: list_* returns ``[]``, in-scope targeted
methods raise ``NotImplementedError``. Real implementations land when the
Spec C tabs that consume them are wired.

Scope construction:

    SurgeonScope("asarin", "sarin", owned_case_ids={"UCD-FIL-001"})
    AdminScope("ankitsarin")

``owned_case_ids`` is a stub seam for the case-ownership check. Spec C
replaces it with a DB lookup against case_manifest.csv / pipeline_state.csv.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from app.exceptions import ScopeViolationError


class UserScope(ABC):
    """Abstract base. Subclasses MUST set ``role`` and implement all methods."""

    role: str
    username: str

    # ----- listing methods (return a list, possibly empty) -----

    @abstractmethod
    def list_raw_segments(self) -> list:
        ...

    @abstractmethod
    def list_concatted_masters(self) -> list:
        ...

    @abstractmethod
    def list_deid_videos(self) -> list:
        ...

    @abstractmethod
    def read_manifest_rows(self) -> list:
        ...

    @abstractmethod
    def list_audit_queue(self) -> list:
        ...

    # ----- targeted reads / writes -----

    @abstractmethod
    def read_case(self, case_id: str):
        ...

    @abstractmethod
    def write_case_metadata(self, case_id: str, **kwargs):
        ...

    @abstractmethod
    def trigger_pipeline(self, case_id: str, stage: str):
        ...

    # ----- admin-only operations -----

    @abstractmethod
    def resolve_audit_flag(self, flag_id: int, **kwargs):
        ...

    @abstractmethod
    def reupload_metadata(self, case_id: str, **kwargs):
        ...


class SurgeonScope(UserScope):
    role = "surgeon"

    def __init__(
        self,
        username: str,
        folder_slug: str,
        owned_case_ids: Iterable[str] | None = None,
    ):
        self.username = username
        self.folder_slug = folder_slug
        self._owned: set[str] = set(owned_case_ids or ())

    def _scope_tag(self) -> str:
        return f"surgeon:{self.folder_slug}"

    def _require_case(self, case_id: str, action: str) -> None:
        if case_id not in self._owned:
            raise ScopeViolationError(
                resource=f"case:{case_id}",
                action=action,
                scope_at_time=self._scope_tag(),
            )

    # listings — stubs in Spec B; Spec C wires DB-backed filters by folder_slug.

    def list_raw_segments(self) -> list:
        return []

    def list_concatted_masters(self) -> list:
        return []

    def list_deid_videos(self) -> list:
        return []

    def read_manifest_rows(self) -> list:
        return []

    def list_audit_queue(self) -> list:
        return []

    # targeted — scope check, then NotImplementedError stub for Spec C.

    def read_case(self, case_id: str):
        self._require_case(case_id, "read_case")
        raise NotImplementedError("read_case body lands in Spec C")

    def write_case_metadata(self, case_id: str, **kwargs):
        self._require_case(case_id, "write_case_metadata")
        raise NotImplementedError("write_case_metadata body lands in Spec C")

    def trigger_pipeline(self, case_id: str, stage: str):
        self._require_case(case_id, "trigger_pipeline")
        raise NotImplementedError("trigger_pipeline body lands in Spec C")

    # admin-only — surgeons cannot reach these, ever.

    def resolve_audit_flag(self, flag_id: int, **kwargs):
        raise ScopeViolationError(
            resource=f"audit_flag:{flag_id}",
            action="resolve_audit_flag",
            scope_at_time=self._scope_tag(),
        )

    def reupload_metadata(self, case_id: str, **kwargs):
        raise ScopeViolationError(
            resource=f"case:{case_id}",
            action="reupload_metadata",
            scope_at_time=self._scope_tag(),
        )


class AdminScope(UserScope):
    role = "admin"

    def __init__(self, username: str):
        self.username = username

    def _scope_tag(self) -> str:
        return "admin"

    # listings — pass-through. Spec C wires unfiltered DB reads.

    def list_raw_segments(self) -> list:
        return []

    def list_concatted_masters(self) -> list:
        return []

    def list_deid_videos(self) -> list:
        return []

    def read_manifest_rows(self) -> list:
        return []

    def list_audit_queue(self) -> list:
        return []

    # All targeted operations available to admin — Spec C wires the bodies.

    def read_case(self, case_id: str):
        raise NotImplementedError("read_case body lands in Spec C")

    def write_case_metadata(self, case_id: str, **kwargs):
        raise NotImplementedError("write_case_metadata body lands in Spec C")

    def trigger_pipeline(self, case_id: str, stage: str):
        raise NotImplementedError("trigger_pipeline body lands in Spec C")

    def resolve_audit_flag(self, flag_id: int, **kwargs):
        raise NotImplementedError("resolve_audit_flag body lands in Spec C")

    def reupload_metadata(self, case_id: str, **kwargs):
        raise NotImplementedError("reupload_metadata body lands in Spec C")
