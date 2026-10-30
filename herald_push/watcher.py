"""Watches every bot's Bot Chat for new replies and pushes them to the registered phones.

Reading the profiles' ``state.db`` (read-only) catches a reply whichever process wrote it: the dashboard,
the messaging gateway, a ``hermes -p <bot> chat`` child for message_agent, or cron. Only one process
watches at a time (a file lock), so phones aren't told twice.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import httpx

from . import cron, push
from .store import Store

log = logging.getLogger(__name__)

PLUGIN_NAME = "herald-push"
BOT_CHAT_TITLE = "Bot Chat"
POLL_SECONDS = 3.0
# After a restart, replies older than this are old news: don't push them.
STALE_SECONDS = 15 * 60
MAX_PER_POLL = 4
# Where the watcher left off is written when a push went out, else at most this often: a lost mark costs
# one duplicate the phone drops, so an fsync per poll during a busy turn isn't worth it.
MARKS_SAVE_SECONDS = 30
_SILENCE = {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY", "[静默]", "静默", "[沉默]", "沉默"}
_LONG_LIVED_MODULES = ("tui_gateway.server", "gateway.run")


def is_long_lived_process(argv: Optional[List[str]] = None, modules: Optional[Dict[str, object]] = None) -> bool:
    """The dashboard or the messaging gateway; not a one-shot chat, cron run or CLI command.

    Decided by what the process has loaded: plugins register lazily, so by then the dashboard
    (``tui_gateway.server``) or the messaging gateway (``gateway.run``) is already imported. The
    command line is only a fallback for launchers that import them later.
    """
    args = [a.lower() for a in (argv if argv is not None else sys.argv)]
    if "-q" in args or "chat" in args:
        return False
    loaded = sys.modules if modules is None else modules
    if any(name in loaded for name in _LONG_LIVED_MODULES):
        return True
    return "dashboard" in args or "serve" in args or ("gateway" in args and "run" in args)


def profile_enabled(home: Path) -> bool:
    """The bot's own "Send notifications" switch: ``plugins.entries.herald-push.settings.enabled`` in its
    profile's config.yaml, where the Plugins hub writes it. Each bot's setting governs that bot's pushes."""
    try:
        import yaml
        data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        entries = ((data.get("plugins") or {}).get("entries") or {}) if isinstance(data, dict) else {}
        for key, entry in entries.items():
            if str(key).rsplit("/", 1)[-1] == PLUGIN_NAME and isinstance(entry, dict):
                return (entry.get("settings") or {}).get("enabled") is not False
    except Exception:
        pass
    return True


def is_silence(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 64:
        return False
    normal = lambda s: re.sub(r"\s+", " ", s.upper()).strip()  # noqa: E731
    if normal(stripped) in _SILENCE:
        return True
    trimmed = stripped.strip("".join(ch for ch in set(stripped) if not ch.isalnum() and not ch.isspace() and ch not in "[]_"))
    return bool(trimmed) and normal(trimmed) in _SILENCE


def plain_text(content: Optional[str]) -> str:
    """A message's words: plain text, or the text parts of a multimodal list."""
    if not content:
        return ""
    stripped = content.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parts = json.loads(stripped)
        except ValueError:
            return stripped
        if isinstance(parts, list):
            return "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text").strip()
    return stripped


def profile_homes(root: Path) -> Iterable[Tuple[str, Path]]:
    yield "default", root
    profiles = root / "profiles"
    if profiles.is_dir():
        for home in sorted(profiles.iterdir()):
            if home.is_dir() and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", home.name):
                yield home.name, home


def bot_chat_tip(home: Path) -> Optional[str]:
    """The live session of the profile's Bot Chat, following compression to its newest continuation."""
    db_path = home / "state.db"
    if not db_path.is_file():
        return None
    try:
        from hermes_state import SessionDB
    except ImportError:
        return None
    db = SessionDB(db_path=db_path, read_only=True)
    try:
        row = db.get_session_by_title(BOT_CHAT_TITLE)
        return db.get_compression_tip(row["id"]) if row else None
    finally:
        db.close()


def bot_label(home: Path, profile: str) -> str:
    """The bot's display name from its Bot Mode metadata (profile.yaml ``ui_meta``), or the profile name."""
    try:
        import yaml
        data = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8")) or {}
        meta = ((data.get("ui_meta") or {}).get("hermes-bots") or {}) if isinstance(data, dict) else {}
        title = meta.get("title") if isinstance(meta, dict) else None
        if isinstance(title, str) and title.strip():
            return title.strip()[:64]
    except Exception:
        pass
    return profile


def new_replies(home: Path, session_id: str, after_id: int) -> Tuple[int, List[Tuple[int, float, str]]]:
    """(newest message id, the bot's final replies after ``after_id`` as (id, timestamp, text))."""
    uri = f"file:{(home / 'state.db').as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        # Compression's summaries and compacted copies aren't new words from the bot.
        carried = " + ".join(c for c in ("_compressed_summary", "compacted") if c in columns) or "0"
        rows = conn.execute(
            f"SELECT id, role, content, tool_calls, active, display_kind, timestamp, ({carried}) FROM messages "
            "WHERE session_id = ? AND id > ? ORDER BY id LIMIT 200",
            (session_id, after_id),
        ).fetchall()
    finally:
        conn.close()
    newest = after_id
    replies = []
    for msg_id, role, content, tool_calls, active, display_kind, ts, carried_over in rows:
        newest = max(newest, int(msg_id))
        if carried_over or role != "assistant" or not active or (tool_calls and tool_calls.strip() not in ("", "[]", "null")):
            continue
        if display_kind and display_kind not in ("assistant", "message", "text"):
            continue
        text = plain_text(content)
        if text and not is_silence(text):
            replies.append((int(msg_id), float(ts or 0), text))
    return newest, replies


def latest_id(home: Path, session_id: str) -> int:
    uri = f"file:{(home / 'state.db').as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        row = conn.execute("SELECT MAX(id) FROM messages WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0) if row else 0


class _LeaderLock:
    """A non-blocking exclusive lock on a file: held while this process is the one watching."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


class Watcher:
    def __init__(self, root: Path, store: Store, enabled: Callable[[Path], bool] = profile_enabled,
                 now: Callable[[], float] = time.time):
        self.root = root
        self.store = store
        self.enabled = enabled
        self.now = now
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = _LeaderLock(store.root / "leader.lock")
        self._marks: Dict[str, int] = {}
        self._marks_dirty = False
        self._marks_saved_at = 0.0
        self._tips: Dict[str, str] = {}
        # Per bot: the texts pushed lately and when, so a compression copy isn't pushed twice.
        self._pushed: Dict[str, Dict[str, float]] = {}
        self.follows = cron.Follows(store)
        self._cron = cron.CronWatcher(now=lambda: self.now())
        self._cron_marks: Dict[str, str] = {}
        self._cron_marks_loaded = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="herald-push-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None
        if self._marks_dirty:
            try:
                self.store.save_marks(self._marks)
            except OSError:
                pass
        self._lock.release()

    def _run(self) -> None:
        # Another process may already be watching; keep trying, so this one takes over if that one exits.
        while not self._stop.is_set():
            if self._lock.acquire():
                break
            self._stop.wait(30)
        if self._stop.is_set():
            return
        self._marks = self.store.marks()
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False) as client:
            while not self._stop.is_set():
                try:
                    self.poll(client)
                except Exception:  # never let one bad poll end the watcher
                    log.exception("herald-push: poll failed")
                self._stop.wait(POLL_SECONDS)

    def poll(self, client: Optional[httpx.Client] = None) -> int:
        """One pass over every bot and every cron job; returns how many pushes went out."""
        devices = self.store.devices()
        sent = self.poll_cron(devices, client)
        changed = False
        for profile, home in profile_homes(self.root):
            try:
                tip = bot_chat_tip(home)
            except Exception:
                log.debug("herald-push: can't read %s", profile, exc_info=True)
                continue
            if not tip:
                continue
            key = f"{profile}:{tip}"
            previous_tip = self._tips.get(profile)
            self._tips[profile] = tip
            if key not in self._marks:
                # A chat seen for the first time starts from now; one that replaced the previous tip
                # (compression) is read from its start: staleness, the carried-over flags and the
                # recently-pushed texts keep old rows out.
                self._marks[key] = 0 if previous_tip and previous_tip != tip else latest_id(home, tip)
                changed = True
                if not (previous_tip and previous_tip != tip):
                    continue
            newest, replies = new_replies(home, tip, self._marks[key])
            if newest != self._marks[key]:
                self._marks[key] = newest
                changed = True
            fresh = [r for r in replies if self.now() - r[1] <= STALE_SECONDS]
            if previous_tip and previous_tip != tip:
                # A new tip after compression may carry copies of the last replies: they went out already.
                pushed = self._pushed.get(profile, {})
                fresh = [r for r in fresh if self.now() - pushed.get(r[2], -STALE_SECONDS * 2) > STALE_SECONDS]
            fresh = fresh[-MAX_PER_POLL:]
            if not fresh or not devices or not self.enabled(home):
                continue
            _, sig = self.store.identity()[1:]
            label = bot_label(home, profile)
            pushed = self._pushed.setdefault(profile, {})
            for _msg_id, _ts, text in fresh:
                pushed[text] = self.now()
                if len(pushed) > 16:
                    del pushed[next(iter(pushed))]
                payload = push.message("bot_message", bot=profile, label=label, session=tip, text=push.clip(text))
                for device in devices:
                    if push.publish(device, payload, sig, client):
                        sent += 1
        if changed:
            # Drop marks of chats that are no longer anyone's tip.
            live = {f"{p}:{t}" for p, t in self._tips.items()}
            self._marks = {k: v for k, v in self._marks.items() if k in live}
            self._marks_dirty = True
        if self._marks_dirty and (sent or self.now() - self._marks_saved_at >= MARKS_SAVE_SECONDS):
            self.store.save_marks(self._marks)
            self._marks_dirty = False
            self._marks_saved_at = self.now()
        return sent

    def poll_cron(self, devices, client: Optional[httpx.Client] = None) -> int:
        """Finished cron runs, to each phone that follows the job (or every failure, when it asked)."""
        if not self._cron_marks_loaded:
            self._cron_marks = self.store.cron_marks()
            self._cron_marks_loaded = True
        runs, changed = self._cron.poll(profile_homes(self.root), self._cron_marks)
        if changed:
            self.store.save_cron_marks(self._cron_marks)
        if not runs or not devices:
            return 0
        follows = self.follows.all()
        _, sig = self.store.identity()[1:]
        sent = 0
        for run in runs:
            for device in devices:
                notify = follows.get(device.device_id, cron.Follow({})).wants(run.job_id, run.status)
                if notify is None:
                    continue
                payload = push.message(
                    "cron_run", bot=run.profile, job=run.job_id, run=run.run, label=run.name,
                    status=run.status, notify=notify, text=push.clip(run.text), at=int(run.at), session=run.session,
                )
                if push.publish(device, payload, sig, client):
                    sent += 1
        return sent
