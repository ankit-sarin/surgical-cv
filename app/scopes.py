"""UserScope hierarchy — the authoritative read/write surface for HTTP handlers.

Three classes:

- ``UserScope`` — abstract base declaring the full method surface from v18.
- ``SurgeonScope(username, folder_slug, repos)`` — listings delegate to the
  appropriate repo (segments via ``repos.segment``, manifest-derived
  listings via ``repos.case``); case-scoped methods delegate ownership
  checks to ``repos.case.case_belongs_to`` and raise
  ``ScopeViolationError`` for unowned ids; admin-only methods
  (resolve_audit_flag, reupload_metadata) always raise.
- ``AdminScope(username, repos)`` — pass-through; listings return ``[]`` for
  now (future specs add a repo-level ``list_all``-style method per kind and
  call it here), targeted methods raise ``NotImplementedError`` (bodies land
  alongside the specs consuming them).

Both subclasses take a ``Repos`` bundle so future repos (pipeline state,
attention items, etc.) land in one place; constructors don't grow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.exceptions import ScopeViolationError
from app.repos import Repos


class UserScope(ABC):
    """Abstract base. Subclasses MUST set ``role`` and implement all methods."""

    role: str
    username: str

    # ----- listing methods -----

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

    def __init__(self, username: str, folder_slug: str, repos: Repos):
        self.username = username
        self.folder_slug = folder_slug
        self._repos = repos

    def _scope_tag(self) -> str:
        return f"surgeon:{self.folder_slug}"

    def _require_case(self, case_id: str, action: str) -> None:
        if not self._repos.case.case_belongs_to(case_id, self.folder_slug):
            raise ScopeViolationError(
                resource=f"case:{case_id}",
                action=action,
                scope_at_time=self._scope_tag(),
            )

    # Listings: raw-segments uses the segment repo (filesystem); the manifest-
    # derived listings still go through the case repo. Semantic differentiation
    # between concat-masters / deid-videos / manifest-rows / audit-queue lands
    # when each tab spec adds a repo method.

    def list_raw_segments(self) -> list:
        return self._repos.segment.list_raw_segments(self.folder_slug)

    def list_concatted_masters(self) -> list:
        return self._repos.case.list_owned_by(self.folder_slug)

    def list_deid_videos(self) -> list:
        return self._repos.case.list_owned_by(self.folder_slug)

    def read_manifest_rows(self) -> list:
        return self._repos.case.list_owned_by(self.folder_slug)

    def list_audit_queue(self) -> list:
        return self._repos.case.list_owned_by(self.folder_slug)

    # Case-scoped — repo decides in-scope; in-scope methods stub-raise.

    def read_case(self, case_id: str):
        self._require_case(case_id, "read_case")
        raise NotImplementedError("read_case body lands in a future spec")

    def write_case_metadata(self, case_id: str, **kwargs):
        self._require_case(case_id, "write_case_metadata")
        raise NotImplementedError(
            "write_case_metadata body lands in a future spec"
        )

    def trigger_pipeline(self, case_id: str, stage: str):
        self._require_case(case_id, "trigger_pipeline")
        raise NotImplementedError(
            "trigger_pipeline body lands in a future spec"
        )

    # Admin-only — surgeons cannot reach these, ever.

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

    def __init__(self, username: str, repos: Repos):
        self.username = username
        self._repos = repos

    def _scope_tag(self) -> str:
        return "admin"

    # Listings: pass-through. Spec C stub returns []; future specs add the
    # appropriate "list-all" repo methods and call them here.

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

    def read_case(self, case_id: str):
        raise NotImplementedError("read_case body lands in a future spec")

    def write_case_metadata(self, case_id: str, **kwargs):
        raise NotImplementedError(
            "write_case_metadata body lands in a future spec"
        )

    def trigger_pipeline(self, case_id: str, stage: str):
        raise NotImplementedError(
            "trigger_pipeline body lands in a future spec"
        )

    def resolve_audit_flag(self, flag_id: int, **kwargs):
        raise NotImplementedError(
            "resolve_audit_flag body lands in a future spec"
        )

    def reupload_metadata(self, case_id: str, **kwargs):
        raise NotImplementedError(
            "reupload_metadata body lands in a future spec"
        )
