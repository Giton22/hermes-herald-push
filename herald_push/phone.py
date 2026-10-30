"""Phone tools: agent tools that run on a phone with Herald.

The tools are a fixed list, reviewed here. A tool is offered to the agent only while a phone says it has it
switched on (``/herald-push phone tools``); every tool starts off on the phone. Hermes fixes a chat's tools
when the chat starts, so a switch changes what new chats are offered; a chat that already has a tool the
user since turned off gets a refusal when it calls it.

A call is a request file in ``<data>/phone/``. The phone learns about it from the agent's tool event (when
it watches that chat), from a push (when it registered for push), or when it connects, reads it with
``/herald-push phone pending`` and answers with ``/herald-push phone answer``. The tool waits for the answer
file, and after its timeout tells the agent the phone didn't answer. Files, not memory: the agent can run in
another process (turn isolation, cron, the messaging gateway) than the dashboard the phone talks to.

Each phone holds a secret key; the gateway keeps only its hash, and every phone command must carry the key,
so one dashboard client can't read or answer another phone's calls. Phone tools refuse turns on messaging
platforms: someone in a group chat with the bot must not get the owner's location or contacts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .store import DEVICE_ID_RE, Store, _write_private

log = logging.getLogger(__name__)

TOOLSET = "herald_phone"

# A phone not heard from for this long no longer offers its tools: Herald was removed, or signed out.
STALE_SECONDS = 14 * 24 * 3600
# How often a waiting tool looks for the answer.
POLL_SECONDS = 0.25
# Requests left behind by a process that died are removed after this long.
LEFTOVER_SECONDS = 3600
MAX_RESULT = 60000
MAX_NAME = 64
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
KEY_RE = re.compile(r"^[0-9a-f]{32,128}$")
# How often each process looks whether a phone changed its tools, to refresh its tool list.
WATCH_SECONDS = 2.0
# Which phone sent a chat's messages is kept this long, for at most this many chat connections.
HERE_SECONDS = 3 * 24 * 3600
MAX_HERE = 500
# A turn with no chat connection (a side question, a background task) goes to a phone that sent this recently.
JUST_SENT_SECONDS = 120
# A chat connection id from the gateway (``HERMES_UI_SESSION_ID``): short, plain characters only.
SID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

# How long the agent waits. Reads answer at once; a change waits for you to allow it on the phone.
READ_TIMEOUT = 30
CONFIRM_TIMEOUT = 120
# A call rings 45 seconds on the phone; the rest is for the push to get there and the answer to come back.
CALL_TIMEOUT = 75
# A screen in the chat waits for the user to look and tap.
SCREEN_TIMEOUT = 300
MAX_SCREEN_MESSAGES = 100
SHOW_UI_TOOL = "phone_show_ui"

_SCREEN = (
    "Use this, not a list in text or an HTML page, when you ask the user to pick between options, confirm, or "
    "fill in a few values. "
    "Shows the user a small screen under your reply in Kyllene on their Android phone, made of A2UI v0.9 messages, "
    "and (with wait, the default) waits up to 5 minutes for them to tap a button. Only for a chat the user has "
    "open in Kyllene on Android: from anywhere else it fails within seconds, so ask in words then. For choices, "
    "confirmations and short forms; keep it small and still say the question in your reply. Returns {event, "
    "surface_id, component_id, context, data} for the button tapped (data: the form's values, when createSurface "
    "had sendDataModel true), {shown: true} with wait false (then a tap comes later as the user's next message, "
    "starting \"Tapped\"), or an error to fix and try again. Messages, in order "
    "(SURFACE is an id you make up, new for each call): "
    '{"version":"v0.9","createSurface":{"surfaceId":"SURFACE","catalogId":'
    '"https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json","sendDataModel":true}}, then '
    '{"version":"v0.9","updateComponents":{"surfaceId":"SURFACE","components":[...]}} with one component whose id '
    'is "root"; optionally {"version":"v0.9","updateDataModel":{"surfaceId":"SURFACE","path":"/","value":{...}}} '
    "for starting values. Components are a flat list, each {id, component, ...props}, children named by id: "
    "Column and Row {children:[ids], justify, align}, Card {child}, Divider, "
    "Text {text, variant: h1-h5, body or caption}, Icon {name}, Button {child: a Text's id, variant: default, "
    'primary or borderless, action: {"event":{"name":"...","context":{...}}}}, TextField {label, value, variant: '
    "shortText, longText, number or obscured}, CheckBox {label, value}, ChoicePicker {label, options: "
    "[{label, value}], value: a list, variant: mutuallyExclusive or multipleSelection}, Slider {label, min, max, "
    "value}, DateTimeInput {label, value, enableDate, enableTime}, Tabs {tabs:[{title, child}]}. Bind a form "
    'field to the data with value: {"path":"/name"}. Pictures, video and audio aren\'t shown. A call is the whole '
    "screen."
)

# The agent rings the user's own phone. It never takes a number: the phone it rings is the user's own Herald.
CALL_TOOL = "phone_call_me"
MAX_REASON = 300
MAX_DETAILS = 2000

# What the agent reads with each way a call can end.
CALL_NOTES = {
    "answered": "The user picked up. They're talking with Hermes by voice now, and the call opened with your "
                "reason. What they say comes in as their next messages. End this turn with one short line; don't "
                "repeat the reason.",
    "declined": "The user declined the call. If it still matters, reach them another way now, for example a "
                "message or a reminder.",
    "no_answer": "The user didn't pick up. If it still matters, reach them another way now, for example a "
                 "message or a reminder.",
    "busy": "The user is already on a call with Hermes, so it didn't ring. If it still matters, send a message "
            "instead.",
    "quiet": "The user set quiet hours on their phone, so it didn't ring, and another call won't ring before they "
             "end. Reach them another way now: send a message, or set a reminder for after quiet hours. Only if it "
             "truly can't wait, start your message by saying it's urgent.",
}
# How the phone gives the end of quiet hours: "07:00".
_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
UNREACHABLE_NOTE = ("The call didn't get through to the user's phone. If it still matters, reach them another way "
                    "now, for example a message or a reminder.")


def _schema(name: str, description: str, properties: dict, required: Optional[List[str]] = None) -> dict:
    params: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


_ISO = "Local date and time, ISO 8601 (2026-10-14T09:30). Without a time, the start of that day."

# name -> (schema, timeout in seconds, emoji)
TOOLS: Dict[str, tuple] = {
    "phone_battery": (_schema(
        "phone_battery",
        "The user's phone battery: level in percent, whether it's charging and from what, and the estimated "
        "time until full when the phone knows it.",
        {}), READ_TIMEOUT, "🔋"),
    "phone_network": (_schema(
        "phone_network",
        "How the user's phone is connected: Wi-Fi, mobile data, ethernet or offline, whether the connection is "
        "metered, and whether a VPN is on.",
        {}), READ_TIMEOUT, "📶"),
    "phone_sound": (_schema(
        "phone_sound",
        "Read or change the user's phone sound: Do Not Disturb, the ringer mode and stream volumes. A change "
        "is shown on the phone and only happens if the user allows it.",
        {
            "action": {"type": "string", "enum": ["get", "set"]},
            "dnd": {"type": "string", "enum": ["off", "priority", "alarms", "silence"],
                    "description": "Do Not Disturb, for action=set."},
            "ringer": {"type": "string", "enum": ["normal", "vibrate", "silent"], "description": "For action=set."},
            "stream": {"type": "string", "enum": ["ring", "notification", "media", "alarm"],
                       "description": "Which volume to set, with volume."},
            "volume": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Percent, with stream."},
        }, ["action"]), CONFIRM_TIMEOUT, "🔔"),
    "phone_alarm": (_schema(
        "phone_alarm",
        "Set an alarm or a timer in the user's phone clock app. Shown on the phone and only set if the user "
        "allows it.",
        {
            "kind": {"type": "string", "enum": ["alarm", "timer"]},
            "hour": {"type": "integer", "minimum": 0, "maximum": 23, "description": "Alarm hour, 24-hour clock."},
            "minute": {"type": "integer", "minimum": 0, "maximum": 59, "description": "Alarm minute."},
            "days": {"type": "array", "items": {"type": "string",
                                                "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                     "description": "Repeat on these days; leave out for a one-time alarm."},
            "seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "description": "Timer length."},
            "label": {"type": "string", "maxLength": 80},
        }, ["kind"]), CONFIRM_TIMEOUT, "⏰"),
    "phone_screen_time": (_schema(
        "phone_screen_time",
        "Which apps the user used on the phone and for how long, today or over the last few days, most used "
        "first.",
        {"days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "1 is today only."}}),
        READ_TIMEOUT, "📱"),
    "phone_open": (_schema(
        "phone_open",
        "Open something on the user's phone: a web link, a place or route in the maps app, an installed app, or "
        "the share sheet with some text. Shown on the phone and only opened if the user allows it.",
        {
            "kind": {"type": "string", "enum": ["url", "map", "app", "share"]},
            "value": {"type": "string", "maxLength": 2000,
                      "description": "The https link, the place or address, the app's name, or the text to share."},
            "navigate": {"type": "boolean", "description": "For kind=map: start directions to the place."},
        }, ["kind", "value"]), CONFIRM_TIMEOUT, "📲"),
    "phone_clipboard": (_schema(
        "phone_clipboard",
        "Put text on the user's phone clipboard, ready to paste. Shown on the phone, all of it, and only copied "
        "if the user allows it; at most 300 characters, so the user can read it first.",
        {"text": {"type": "string", "maxLength": 300}}, ["text"]), CONFIRM_TIMEOUT, "📋"),
    "phone_location": (_schema(
        "phone_location",
        "Where the user's phone is: latitude, longitude, accuracy in meters, when it was measured, and the "
        "nearest address when the phone can look it up.",
        {"fresh": {"type": "boolean",
                   "description": "Measure now instead of using the last known place (slower)."}}),
        READ_TIMEOUT, "📍"),
    "phone_geofence": (_schema(
        "phone_geofence",
        "Places the phone watches: when the user arrives at or leaves one, the phone sends the given message "
        "to this chat as the user. Add (around a point, or where the phone is now when latitude and longitude "
        "are left out), list, or remove. Adding is shown on the phone and only happens if the user allows it.",
        {
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "name": {"type": "string", "maxLength": 60, "description": "What the place is called, e.g. Home."},
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
            "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
            "trigger": {"type": "string", "enum": ["enter", "exit"]},
            "message": {"type": "string", "maxLength": 500,
                        "description": "What the phone sends to this chat when it fires."},
            "id": {"type": "string", "maxLength": 64, "description": "For action=remove, from action=list."},
        }, ["action"]), CONFIRM_TIMEOUT, "🗺️"),
    "phone_calendar": (_schema(
        "phone_calendar",
        "The user's phone calendar: list events in a time range, add an event, or move one. Adding and moving "
        "are shown on the phone and only happen if the user allows it.",
        {
            "action": {"type": "string", "enum": ["list", "add", "move"]},
            "from": {"type": "string", "description": "For list. " + _ISO},
            "to": {"type": "string", "description": "For list. " + _ISO},
            "title": {"type": "string", "maxLength": 200},
            "start": {"type": "string", "description": _ISO},
            "end": {"type": "string", "description": _ISO},
            "all_day": {"type": "boolean"},
            "location": {"type": "string", "maxLength": 200},
            "notes": {"type": "string", "maxLength": 2000},
            "event_id": {"type": "string", "maxLength": 32, "description": "For action=move, from action=list."},
        }, ["action"]), CONFIRM_TIMEOUT, "📅"),
    "phone_contacts": (_schema(
        "phone_contacts",
        "Look up people in the user's phone contacts by name, number or email: their names, phone numbers and "
        "email addresses.",
        {
            "query": {"type": "string", "maxLength": 100},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }, ["query"]), READ_TIMEOUT, "👤"),
    "phone_health": (_schema(
        "phone_health",
        "The user's health data from the phone's Health Connect: steps, sleep sessions or heart rate over a "
        "time range.",
        {
            "metric": {"type": "string", "enum": ["steps", "sleep", "heart_rate"]},
            "from": {"type": "string", "description": _ISO},
            "to": {"type": "string", "description": _ISO},
        }, ["metric"]), READ_TIMEOUT, "❤️"),
    CALL_TOOL: (_schema(
        CALL_TOOL,
        "Ring the user's phone (and watch) like an incoming call. When they pick up, Hermes talks with them by "
        "voice, starting with your reason. It only ever calls the user, never anyone else. Use it when something is "
        "important or urgent enough to interrupt them (an alert, a reminder that must not be missed, a decision "
        "that can't wait), or when the user asked to be called, also from a scheduled job (\"call me at 7:30\"). "
        "Waits until the call is answered, declined or not answered (about 45 seconds of ringing) and returns that "
        "status. It doesn't ring in the user's quiet hours. When it isn't answered, reach the user another way.",
        {
            "reason": {"type": "string", "maxLength": MAX_REASON,
                       "description": "Why you call, in one or two sentences. Hermes opens the call with it."},
            "details": {"type": "string", "maxLength": MAX_DETAILS,
                        "description": "Optional background for the call: what happened, deadlines, the options."},
        }, ["reason"]), CALL_TIMEOUT, "📞"),
    SHOW_UI_TOOL: (_schema(
        SHOW_UI_TOOL,
        _SCREEN,
        {
            "messages": {"type": "array", "minItems": 1, "maxItems": MAX_SCREEN_MESSAGES,
                         "items": {"type": "object"}, "description": "A2UI messages, in the order they apply."},
            "wait": {"type": "boolean", "description": "Wait for a tap (default true); false only shows it."},
        }, ["messages"]), SCREEN_TIMEOUT, "🧩"),
}


def call_result(raw: str) -> str:
    """The phone's answer to a call, with what the agent should do next. Anything but a status the phone gave
    (no phone, no answer in time) means the call didn't get through."""
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    status = data.get("status") if isinstance(data, dict) else None
    if status in CALL_NOTES:
        out = {"status": status, "note": CALL_NOTES[status]}
        until = data.get("until")
        if status == "quiet" and isinstance(until, str) and _CLOCK.fullmatch(until):
            out["until"] = until
            out["note"] += f" Quiet hours end at {until}, the phone's local time."
        return json.dumps(out)
    error = data.get("error") if isinstance(data, dict) else None
    out = {"status": "unreachable", "note": UNREACHABLE_NOTE}
    if isinstance(error, str):
        out["error"] = error
    return json.dumps(out)


def call_args(args: dict) -> Optional[dict]:
    """[args] of a call, trimmed; None without a reason."""
    reason = args.get("reason") if isinstance(args, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        return None
    out = {"reason": reason.strip()[:MAX_REASON]}
    details = args.get("details")
    if isinstance(details, str) and details.strip():
        out["details"] = details.strip()[:MAX_DETAILS]
    return out


class Phones:
    """Which phones offer which tools, and the requests waiting for them, in ``<data>/phone/``."""

    def __init__(self, store: Store, wake: Optional[Callable[[str, dict], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.root = store.root / "phone"
        self.wake = wake
        self.clock = clock

    # ── which phones offer what ─────────────────────────────────────────────

    def _phones(self) -> Dict[str, dict]:
        try:
            raw = json.loads((self.root / "phones.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode("ascii")).hexdigest()

    def verify(self, device_id: str, key: str) -> bool:
        """Whether [key] is the secret [device_id] reported with."""
        if not DEVICE_ID_RE.match(device_id or "") or not KEY_RE.match(key or ""):
            return False
        known = self._phones().get(device_id, {}).get("key")
        return isinstance(known, str) and hmac.compare_digest(known, self._hash(key))

    def report(self, device_id: str, key: str, name: str, tools: List[str], push_id: Optional[str] = None) -> List[str]:
        """Records what [device_id] has switched on (only tools this plugin knows); returns those. The first
        report binds the phone id to [key]; a later one with another key is refused. [push_id] is the phone's
        push registration, if any, which a call nudges. Only a ring ([CALL_TOOL]) travels in the push itself, so
        the phone rings while it can't reach the gateway; every other call is fetched over the socket."""
        if not DEVICE_ID_RE.match(device_id or ""):
            raise ValueError("bad device_id")
        if not KEY_RE.match(key or ""):
            raise ValueError("bad key")
        if push_id is not None and not DEVICE_ID_RE.match(push_id):
            raise ValueError("bad push_id")
        kept = sorted({t for t in tools if isinstance(t, str) and t in TOOLS})
        clean = "".join(ch for ch in str(name or "") if ch.isprintable())[:MAX_NAME].strip() or "Phone"
        with self.store._lock():
            phones = self._phones()
            known = phones.get(device_id, {}).get("key")
            if isinstance(known, str) and not hmac.compare_digest(known, self._hash(key)):
                raise ValueError("this phone id is held by another key")
            # Kept with no tools too: the key stays bound to the id.
            entry = {"name": clean, "tools": kept, "seen": int(self.clock()), "key": self._hash(key), "push": push_id}
            rank = phones.get(device_id, {}).get("rank")
            if isinstance(rank, int):
                entry["rank"] = rank
            phones[device_id] = entry
            _write_private(self.root / "phones.json", phones)
        _refresh_tool_list()
        # What is still waiting for a tool that's off now is refused.
        for request in self.pending(device_id):
            if request["tool"] not in kept:
                self.answer(device_id, request["id"], None, "The user turned this tool off on the phone.")
        return kept

    # ── which phone a call goes to ──────────────────────────────────────────

    def _here(self) -> dict:
        raw = self._read(self.root / "here.json") or {}
        sids = raw.get("sids") if isinstance(raw.get("sids"), dict) else {}
        used = raw.get("used") if isinstance(raw.get("used"), dict) else {}
        return {"sids": sids, "used": used}

    def here(self, device_id: str, sid: str) -> bool:
        """[device_id] sends messages on chat connection [sid]: calls from turns there go to it. Kept apart from
        ``phones.json``, whose every change makes each process build its tool list again."""
        if not SID_RE.match(sid or ""):
            return False
        now = int(self.clock())
        with self.store._lock():
            here = self._here()
            sids = {k: v for k, v in here["sids"].items()
                    if isinstance(v, dict) and now - int(v.get("at", 0)) < HERE_SECONDS}
            sids[sid] = {"device": device_id, "at": now}
            if len(sids) > MAX_HERE:
                sids = dict(sorted(sids.items(), key=lambda kv: int(kv[1].get("at", 0)))[-MAX_HERE:])
            used = {k: v for k, v in here["used"].items() if k in self._phones()}
            used[device_id] = now
            _write_private(self.root / "here.json", {"sids": sids, "used": used})
        return True

    def _ordered(self, phones: Dict[str, dict], used: dict) -> List[str]:
        """[phones] in the user's order: those placed first, then the rest by last use, then last report."""
        return sorted(phones, key=lambda k: (
            phones[k].get("rank") if isinstance(phones[k].get("rank"), int) else MAX_HERE,
            -int(used.get(k, 0)), -int(phones[k].get("seen", 0)), k))

    def listing(self, me: str) -> List[dict]:
        """Every phone the gateway knows, in the order calls no phone started try them; [me] is marked. Ids, names
        and switches only: never another phone's key or push registration."""
        phones = self._phones()
        used = self._here()["used"]
        return [{"id": k, "name": phones[k].get("name") or "Phone", "tools": phones[k].get("tools") or [],
                 "seen": int(phones[k].get("seen", 0)), "used": int(used.get(k, 0)), "me": k == me}
                for k in self._ordered(phones, used)]

    def order(self, ids: List[str]) -> bool:
        """Puts the phones [ids] first, in that order; the others follow by last use."""
        if not all(isinstance(i, str) for i in ids):
            return False
        with self.store._lock():
            phones = self._phones()
            for v in phones.values():
                v.pop("rank", None)
            for rank, device_id in enumerate(i for i in dict.fromkeys(ids) if i in phones):
                phones[device_id]["rank"] = rank
            _write_private(self.root / "phones.json", phones)
        return True

    def remove(self, device_id: str) -> bool:
        """Forgets [device_id]: a phone the user doesn't have any more. Its key goes with it; if it's still in
        use, its next report adds it again as new."""
        with self.store._lock():
            phones = self._phones()
            if device_id not in phones:
                return False
            del phones[device_id]
            _write_private(self.root / "phones.json", phones)
            here = self._here()
            sids = {k: v for k, v in here["sids"].items() if isinstance(v, dict) and v.get("device") != device_id}
            used = {k: v for k, v in here["used"].items() if k != device_id}
            _write_private(self.root / "here.json", {"sids": sids, "used": used})
        _refresh_tool_list()
        for request in self.pending(device_id):
            self.answer(device_id, request["id"], None, "The user removed that phone.")
        return True

    def route(self, tool: str, sid: str = "") -> tuple:
        """``(phone, refusal)`` for a call of [tool] from a turn on chat connection [sid]:
        the phone that sent that turn's message; else a phone that sent a turn in the last two minutes; else the
        first phone in the user's order that has the tool on.
        The phone that sent the message decides alone: with the tool off there, no other phone is asked."""
        now = self.clock()
        phones = {k: v for k, v in self._phones().items() if now - int(v.get("seen", 0)) < STALE_SECONDS}

        def offers(device_id: Optional[str]) -> bool:
            return device_id in phones and tool in (phones[device_id].get("tools") or [])

        here = self._here()
        sender = (here["sids"].get(sid) or {}).get("device") if sid else None
        if sender in phones:
            if offers(sender):
                return sender, None
            return None, f"The phone this message came from ({phones[sender].get('name') or 'Phone'}) has this tool off."
        offering = [(int(here["used"].get(k, 0)), int(v.get("seen", 0)), k) for k, v in phones.items() if offers(k)]
        # A side question or background task (/btw, /bg) runs without its chat connection: the phone that sent
        # a turn moments ago is the one that asked.
        latest = max(offering) if offering else None
        if latest is not None and now - latest[0] < JUST_SENT_SECONDS:
            return latest[2], None
        first = next((k for k in self._ordered(phones, here["used"]) if offers(k)), None)
        return (first, None) if first else (None, "No phone offers this tool any more.")

    def phone_for(self, tool: str, sid: str = "") -> Optional[str]:
        """The phone a call of [tool] from chat connection [sid] goes to, or None."""
        return self.route(tool, sid)[0]

    def offered(self, tool: str) -> bool:
        """Whether any phone has [tool] on: what the agent is offered."""
        now = self.clock()
        return any(tool in (v.get("tools") or []) and now - int(v.get("seen", 0)) < STALE_SECONDS
                   for v in self._phones().values())

    # ── requests ────────────────────────────────────────────────────────────

    def _path(self, request_id: str, kind: str) -> Path:
        return self.root / f"{request_id}.{kind}.json"

    def call(self, tool: str, args: dict, session_id: str = "", sid: str = "") -> str:
        """Runs [tool] on a phone and waits for its answer: the tool result the agent reads, as JSON. [sid] is
        the chat connection whose turn calls it, which picks the phone (see [route])."""
        device_id, refusal = self.route(tool, sid)
        if device_id is None:
            return _error(refusal or "No phone offers this tool any more.")
        timeout = TOOLS[tool][1]
        request_id = uuid.uuid4().hex
        now = self.clock()
        request = {"id": request_id, "device_id": device_id, "tool": tool, "args": args if isinstance(args, dict) else {},
                   "session": str(session_id or ""), "created": int(now), "expires": int(now + timeout)}
        _write_private(self._path(request_id, "req"), request)
        # Read after writing: a remove that ran in between has either seen this request (and answers it) or
        # is seen here; either way the call doesn't wait out its timeout for a phone that's gone.
        phone = self._phones().get(device_id)
        if phone is None and self._read(self._path(request_id, "res")) is None:
            self._path(request_id, "req").unlink(missing_ok=True)
            return _error("The user removed that phone.")
        push_id = (phone or {}).get("push")
        if self.wake is not None and isinstance(push_id, str):
            # The push goes out beside the wait: ntfy may take seconds, and the phone may already be listening.
            threading.Thread(target=self._wake, args=(push_id, request), daemon=True).start()
        try:
            deadline = now + timeout
            while self.clock() < deadline:
                answer = self._read(self._path(request_id, "res"))
                if answer is not None:
                    if answer.get("ok"):
                        return json.dumps(answer.get("result"), ensure_ascii=False)
                    return _error(str(answer.get("error") or "The phone couldn't do it."))
                if self.interrupted():
                    return _error("Stopped before the phone answered.")
                time.sleep(POLL_SECONDS)
            return _error("The phone didn't answer in time. Kyllene may be closed, offline or not connected to "
                          "this gateway, or the user didn't respond.")
        finally:
            for kind in ("req", "res"):
                _unlink(self._path(request_id, kind))

    def _wake(self, push_id: str, request: dict) -> None:
        try:
            self.wake(push_id, request)
        except Exception:
            log.warning("herald-push: couldn't wake the phone for a tool", exc_info=True)

    @staticmethod
    def _read(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _sweep(self) -> None:
        """Removes what a process that died, or an answer that came too late, left behind."""
        cutoff = time.time() - LEFTOVER_SECONDS
        for pattern in ("*.req.json", "*.res.json", ".tmp-*"):
            for path in self.root.glob(pattern):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    pass

    def pending(self, device_id: str) -> List[dict]:
        """Requests waiting for [device_id], oldest first."""
        if not self.root.is_dir():
            return []
        self._sweep()
        now = self.clock()
        out = []
        for path in sorted(self.root.glob("*.req.json")):
            request = self._read(path)
            if request is None:
                continue
            if request.get("device_id") != device_id or now > int(request.get("expires", 0)):
                continue
            if self._path(str(request.get("id")), "res").exists():
                continue
            out.append({k: request.get(k) for k in ("id", "tool", "args", "session", "expires")})
        return sorted(out, key=lambda r: r["expires"])

    def answer(self, device_id: str, request_id: str, result: object, error: Optional[str]) -> bool:
        """Hands the answer to the waiting tool; only for a request made to [device_id]."""
        if not REQUEST_ID_RE.match(request_id or ""):
            return False
        request = self._read(self._path(request_id, "req"))
        if request is None or request.get("device_id") != device_id:
            return False
        if self.clock() > int(request.get("expires", 0)):
            return False
        body = {"ok": error is None, "result": result} if error is None else {"ok": False, "error": str(error)[:500]}
        if len(json.dumps(body, ensure_ascii=False)) > MAX_RESULT:
            body = {"ok": False, "error": "The phone's answer was too long."}
        path = self._path(request_id, "res")
        _write_private(path, body)
        if not self._path(request_id, "req").exists():
            # The tool gave up while this was written: nobody will read it.
            _unlink(path)
            return False
        return True

    @staticmethod
    def interrupted() -> bool:
        """Whether the user stopped the turn this tool runs in."""
        try:
            from tools.interrupt import is_interrupted
        except ImportError:
            return False
        try:
            return bool(is_interrupted())
        except Exception:
            return False


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _error(text: str) -> str:
    return json.dumps({"error": text})


def _refresh_tool_list() -> None:
    """Makes this process build its tool list again: Hermes caches it, and a check_fn's answer isn't part of
    the cache key. Private names, so each step is optional."""
    try:
        from tools.registry import registry
        with registry._lock:
            registry._generation += 1
    except Exception:
        pass
    try:
        from model_tools import _clear_tool_defs_cache
        _clear_tool_defs_cache()
    except Exception:
        pass


def on_messaging_platform() -> bool:
    """Whether this turn came from a messaging platform (Telegram, Discord, ...), where others can talk to the
    bot. Unknown counts as no: the dashboard, Herald, Desktop, cron and the CLI don't set a platform."""
    try:
        from gateway.session_context import session_is_messaging_surface
    except ImportError:
        return False
    try:
        return bool(session_is_messaging_surface())
    except Exception:
        return False


def turn_connection() -> str:
    """The chat connection whose message started this turn (the dashboard's session id), or "" for a turn no
    one sent: a cron job, a background task, an older Hermes."""
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return ""
    try:
        return str(get_session_env("HERMES_UI_SESSION_ID", "") or "")
    except Exception:
        return ""


class _Watch(threading.Thread):
    """Refreshes this process's tool list when another process records a phone's switches."""

    def __init__(self, path: Path):
        super().__init__(name="herald-push-phone-watch", daemon=True)
        self.path = path
        self.stopped = threading.Event()

    def _stamp(self):
        try:
            st = self.path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def run(self) -> None:
        last = self._stamp()
        while not self.stopped.wait(WATCH_SECONDS):
            now = self._stamp()
            if now != last:
                last = now
                _refresh_tool_list()


_watch_lock = threading.Lock()
_watch: Optional[_Watch] = None


SCREEN_HINT_ID = "kyllene-screens"
SCREEN_HINT = (
    "The user's Kyllene app on Android can show a small screen under your reply with phone_show_ui. When you "
    "ask the user to pick between a few options, confirm or cancel something, or fill in a few values, call "
    "phone_show_ui for it instead of listing the choices in text or writing an HTML page, and still say the "
    "question in your reply. Not for answers, long content or anything to read. If the call says the user can't "
    "see it, ask in words."
)


def messaging_platform(platform: str) -> bool:
    """Whether a chat's [platform] is a messaging one, as Hermes tells them apart; unknown counts as no."""
    ident = str(platform or "").strip().lower()
    if not ident:
        return False
    try:
        from gateway.session_context import NON_MESSAGING_SESSION_SURFACES
    except ImportError:
        return False
    return ident not in NON_MESSAGING_SESSION_SURFACES


def screen_hint(phones: Phones, info=None) -> str:
    """When to draw a screen, for a new chat's instructions: only while a phone has the tool on, as the tool,
    and not on a messaging platform. [info] is the chat's, from Hermes: its platform is known when the
    instructions are built, before any turn has started."""
    if not phones.offered(SHOW_UI_TOOL):
        return ""
    if messaging_platform((info or {}).get("platform", "")) or on_messaging_platform():
        return ""
    return SCREEN_HINT


def register_screen_hint(ctx, phones: Phones) -> None:
    """A tool's description alone rarely makes the agent pick it over text: the chat's instructions say when."""
    register = getattr(ctx, "register_system_prompt_section", None)
    if register is None:  # older Hermes: the tool's description is all the agent reads
        return
    try:
        register(SCREEN_HINT_ID, lambda info: screen_hint(phones, info))
    except (TypeError, ValueError) as e:
        log.warning("herald-push: no screen hint in the chat's instructions: %s", e)


def register_tools(ctx, phones: Phones) -> None:
    """Every phone tool, each offered only while a phone has it on."""
    global _watch
    try:
        from tools.registry import no_cache_check_fn
    except ImportError:  # older Hermes: the check is cached for 30 s
        def no_cache_check_fn(fn):
            return fn

    for name, (schema, _timeout, emoji) in TOOLS.items():
        def handler(args, session_id: str = "", _name=name, **_kwargs):
            if on_messaging_platform():
                return _error("Phone tools only work in chats from Kyllene, Desktop or the dashboard, not on a "
                              "messaging platform.")
            if _name == CALL_TOOL:
                trimmed = call_args(args)
                if trimmed is None:
                    return _error("reason is required.")
                return call_result(phones.call(_name, trimmed, session_id, turn_connection()))
            return phones.call(_name, args, session_id, turn_connection())

        def check(_name=name) -> bool:
            return phones.offered(_name)

        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=no_cache_check_fn(check), emoji=emoji)

    register_screen_hint(ctx, phones)

    with _watch_lock:
        if _watch is None:
            _watch = _Watch(phones.root / "phones.json")
            _watch.start()
            started = _watch

            def stop() -> None:
                global _watch
                with _watch_lock:
                    if _watch is started:
                        _watch.stopped.set()
                        _watch = None

            on_unload = getattr(ctx, "on_unload", None)
            if on_unload is not None:
                on_unload(stop)
