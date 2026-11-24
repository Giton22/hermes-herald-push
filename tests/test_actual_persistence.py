"""Optional integration against an importable Hermes persistence implementation (no live data)."""
import os
import pytest

if os.environ.get("HERALD_TEST_HERMES") != "1":
    pytest.skip("set HERALD_TEST_HERMES=1 with Hermes on PYTHONPATH to run integration", allow_module_level=True)
pytest.importorskip("persistence_assembly", reason="Hermes persistence is unavailable")

from persistence_assembly import open_persistence, profile_scope
from persistence.conversation.sessions import NewSession
from persistence.conversation.messages import NewMessage

from herald_push import push, watcher
from herald_push.store import Store
from tests.test_plugin import phone
from herald_push.store import Device


def test_real_storage_discovery_and_compression(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    with open_persistence(profile_scope(home=home)) as db:
        db.sessions.create(NewSession(session_id='root', source='local'))
        db.sessions.set_title('root', 'Bot Chat')
        db.sessions.create(NewSession(session_id='other', source='local'))
        db.sessions.set_title('other', 'Bot Chat #2')
        assert watcher.bot_chat_tip(home) == 'root'
        store = Store(tmp_path / 'push')
        store.upsert(Device.parse(phone()[0]))
        seen = []
        monkeypatch.setattr(push, 'publish', lambda device, payload, sig, client=None: seen.append(payload['text']) or True)
        w = watcher.Watcher(home, store)
        assert w.poll() == 0
        for n in range(3):
            db.messages.append('root', NewMessage(role='assistant', content=f'reply {n}'))
        for _ in range(5):
            w.poll()
        assert seen == [f'reply {n}' for n in range(3)]
        db.sessions.end('root', 'compression')
        db.sessions.create(NewSession(session_id='next', source='local', parent_session_id='root'))
        assert watcher.bot_chat_tip(home) == 'next'
        db.messages.append('next', NewMessage(role='assistant', content='after compression'))
        w.poll()
        assert seen[-1] == 'after compression'
