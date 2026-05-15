"""UserScope hierarchy — the authoritative read/write surface for HTTP handlers.

Three classes:

- ``UserScope`` — abstract base declaring the full method surface from v18.
- ``SurgeonScope(username, folder_slug, repo)`` — listings delegate to
  ``repo.list_owned_by(folder_slug)``; case-scoped methods delegate ownership
  checks to ``repo.case_belongs_to(...)`` and raise ``ScopeViolationError``
  for unowned ids; admin-only methods (resolve_audit_flag, reupload_metadata)
  always raise ``ScopeViolationError``.
- ``AdminScope(username, repo)`` — pass-through; listings return ``[]`` for
  now (future specs add a repo-level ``list_all`` and call it here), targeted
  methods raise ``NotImplementedError`` (bodies land alongside the Spec C+
  tabs consuming them).

The repo arg is the canonical ``CaseRepository`` Protocol. In tests, pass
``InMemoryCaseRepository({...})`` — no env var, no file I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.exceptions import ScopeViolationError
from app.repos.cases import CaseRepository


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
        self, username: str, folder_slug: str, repo: CaseRepository
    ):
        self.username = username
        self.folder_slug = folder_slug
        self._repo = repo

    def _scope_tag(self) -> str:
        return f"surgeon:{self.folder_slug}"

    def _require_case(self, case_id: str, action: str) -> None:
        if not self._repo.case_belongs_to(case_id, self.folder_slug):
            raise ScopeViolationError(
                resource=f"case:{case_id}",
                action=action,
                scope_at_time=self._scope_tag(),
            )

    # All five listings return the same list of case_ids for now. Semantic
    # differentiation (raw segments vs concat masters vs deid videos vs
    # manifest rows vs audit queue) lands when the tab consuming each is
    # specced — repo gains a method per kind, this scope calls it.

    def list_raw_segments(self) -> list:
        return self._repo.list_owned_by(self.folder_slug)

    def list_concatted_masters(self) -> list:
        return self._repo.list_owned_by(self.folder_slug)

    def list_deid_videos(self) -> list:
        return self._repo.list_owned_by(self.folder_slug)

    def read_manifest_rows(self) -> list:
        return self._repo.list_owned_by(self.folder_slug)

    def list_audit_queue(self) -> list:
        return self._repo.list_owned_by(self.folder_slug)

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

    def __init__(self, username: str, repo: CaseRepository):
        self.username = username
        self._repo = repo

    def _scope_tag(self) -> str:
        return "admin"

    # Listings: pass-through. Spec C stub returns []; future specs add a
    # repo.list_all() and call it here.

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
