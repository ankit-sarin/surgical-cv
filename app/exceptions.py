"""Exceptions raised across the HTTP layer.

``ScopeViolationError`` is raised both at the route boundary (prefix-mismatch
case in role dependencies) and from inside ``UserScope`` methods when a
targeted operation hits an out-of-scope resource. A single FastAPI exception
handler in ``app/main.py`` catches it, writes one row to ``scope_violation_log``,
and returns a generic 403.
"""

from __future__ import annotations


class ScopeViolationError(Exception):
    """A request crossed a UserScope boundary.

    ``resource`` is the thing the user tried to touch (path, case id,
    audit-flag id, etc.). ``action`` is the verb (HTTP method or scope-method
    name). ``scope_at_time`` snapshots the caller's scope: ``"surgeon:<slug>"``
    or ``"admin"``.
    """

    def __init__(self, resource: str, action: str, scope_at_time: str):
        self.resource = resource
        self.action = action
        self.scope_at_time = scope_at_time
        super().__init__(f"scope violation: {action} on {resource}")
