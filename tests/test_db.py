from __future__ import annotations

from bn_monitor.db import session_scope


def test_session_scope_is_async_context_manager() -> None:
    assert hasattr(session_scope, "__wrapped__")
