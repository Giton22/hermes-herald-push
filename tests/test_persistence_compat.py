"""Persistence API compatibility without requiring Hermes in the test environment."""
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from herald_push import watcher


def test_modern_reader_follows_compression(tmp_path, monkeypatch):
    (tmp_path / "state.db").touch()
    calls = []

    @contextmanager
    def reader(path):
        calls.append(path)
        yield SimpleNamespace(
            sessions=SimpleNamespace(resolve_by_title=lambda title: "root" if title == "Bot Chat" else None),
            lineage=SimpleNamespace(tip=lambda session: "continued" if session == "root" else None),
        )
        calls.append("closed")

    monkeypatch.setitem(sys.modules, "persistence_assembly", SimpleNamespace(open_persistence_reader=reader))
    # The deprecated adapter can still import but no longer has the old methods.
    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=lambda **kw: object()))
    assert watcher.bot_chat_tip(tmp_path) == "continued"
    assert calls == [tmp_path / "state.db", "closed"]


def test_legacy_reader_follows_compression_and_closes(tmp_path, monkeypatch):
    (tmp_path / "state.db").touch()
    calls = []

    class Legacy:
        def __init__(self, **kw):
            calls.append(kw)
        def get_session_by_title(self, title):
            assert title == "Bot Chat"
            return {"id": "root"}
        def get_compression_tip(self, session):
            assert session == "root"
            return "continued"
        def close(self):
            calls.append("closed")

    monkeypatch.setitem(sys.modules, "persistence_assembly", None)
    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=Legacy))
    assert watcher.bot_chat_tip(tmp_path) == "continued"
    assert calls == [{"db_path": tmp_path / "state.db", "read_only": True}, "closed"]


def test_missing_database_and_missing_apis(tmp_path, monkeypatch):
    assert watcher.bot_chat_tip(tmp_path) is None
    (tmp_path / "state.db").touch()
    monkeypatch.setitem(sys.modules, "persistence_assembly", None)
    monkeypatch.setitem(sys.modules, "hermes_state", None)
    assert watcher.bot_chat_tip(tmp_path) is None


@pytest.mark.parametrize("modern", [True, False])
def test_no_title_returns_none(tmp_path, monkeypatch, modern):
    (tmp_path / "state.db").touch()
    if modern:
        @contextmanager
        def reader(path):
            yield SimpleNamespace(sessions=SimpleNamespace(resolve_by_title=lambda title: None),
                                  lineage=SimpleNamespace(tip=lambda _: pytest.fail("unexpected tip lookup")))
        monkeypatch.setitem(sys.modules, "persistence_assembly", SimpleNamespace(open_persistence_reader=reader))
    else:
        monkeypatch.setitem(sys.modules, "persistence_assembly", None)
        monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=lambda **kw: SimpleNamespace(
            get_session_by_title=lambda title: None, close=lambda: None)))
    assert watcher.bot_chat_tip(tmp_path) is None


def test_legacy_lookup_error_closes_and_propagates(tmp_path, monkeypatch):
    (tmp_path / "state.db").touch()
    closed = []
    def fail(_):
        raise RuntimeError("lookup failed")
    monkeypatch.setitem(sys.modules, "persistence_assembly", None)
    monkeypatch.setitem(sys.modules, "hermes_state", SimpleNamespace(SessionDB=lambda **kw: SimpleNamespace(
        get_session_by_title=fail, close=lambda: closed.append(True))))
    with pytest.raises(RuntimeError, match="lookup failed"):
        watcher.bot_chat_tip(tmp_path)
    assert closed == [True]


def test_modern_error_propagates_and_closes(tmp_path, monkeypatch):
    (tmp_path / "state.db").touch()
    closed = []
    @contextmanager
    def reader(path):
        try:
            yield SimpleNamespace(sessions=SimpleNamespace(resolve_by_title=lambda _: (_ for _ in ()).throw(RuntimeError("read failed"))))
        finally:
            closed.append(True)
    monkeypatch.setitem(sys.modules, "persistence_assembly", SimpleNamespace(open_persistence_reader=reader))
    with pytest.raises(RuntimeError, match="read failed"):
        watcher.bot_chat_tip(tmp_path)
    assert closed == [True]


def test_missing_nested_dependency_is_not_silenced(tmp_path, monkeypatch):
    (tmp_path / "state.db").touch()
    import builtins
    real_import = builtins.__import__
    def broken(name, *args, **kwargs):
        if name == "persistence_assembly":
            raise ModuleNotFoundError("missing dependency", name="some_dependency")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(ModuleNotFoundError, match="missing dependency"):
        watcher.bot_chat_tip(tmp_path)
