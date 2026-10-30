import json
import os
import sys
import threading
import time
import types

import pytest

from herald_push import commands, phone
from herald_push.phone import Phones
from herald_push.store import Store

PHONE = "0b6f2c1e-1111-4222-8333-944445555666"
OTHER = "99999999-aaaa-bbbb-cccc-dddddddddddd"
KEY = "a" * 64
OTHER_KEY = "b" * 64


def answer_when_asked(phones, device_id, result=None, error=None, wait=5.0):
    """Answers the first request for [device_id], as Herald would, from another thread."""
    def run():
        deadline = time.time() + wait
        while time.time() < deadline:
            waiting = phones.pending(device_id)
            if waiting:
                phones.answer(device_id, waiting[0]["id"], result, error)
                return
            time.sleep(0.02)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_only_known_tools_are_kept_and_offered(tmp_path):
    phones = Phones(Store(tmp_path))
    assert phones.report(PHONE, KEY, "Pixel", ["phone_battery", "rm_rf", 5, "phone_battery"]) == ["phone_battery"]
    assert phones.offered("phone_battery")
    assert not phones.offered("phone_location")


@pytest.mark.parametrize("device_id, key, push_id", [
    ("../../etc", KEY, None), (PHONE, "short", None), (PHONE, "Z" * 64, None), (PHONE, KEY, "../x"),
])
def test_bad_ids_and_keys_are_refused(tmp_path, device_id, key, push_id):
    with pytest.raises(ValueError):
        Phones(Store(tmp_path)).report(device_id, key, "x", ["phone_battery"], push_id)


def test_no_push_registration_no_nudge(tmp_path):
    woken = []
    phones = Phones(Store(tmp_path), wake=lambda d, r: woken.append((d, r)))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    answer_when_asked(phones, PHONE, result={})
    phones.call("phone_battery", {})
    assert woken == []


def test_another_key_cant_take_over_a_phone(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    with pytest.raises(ValueError):
        phones.report(PHONE, OTHER_KEY, "Thief", [])
    # Turning everything off keeps the key bound to the id.
    phones.report(PHONE, KEY, "Pixel", [])
    with pytest.raises(ValueError):
        phones.report(PHONE, OTHER_KEY, "Thief", ["phone_battery"])
    assert phones.verify(PHONE, KEY) and not phones.verify(PHONE, OTHER_KEY)
    assert "key" not in json.dumps(phones.pending(PHONE))
    stored = (tmp_path / "phone" / "phones.json").read_text()
    assert KEY not in stored


def test_a_phone_not_heard_from_stops_offering(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    now[0] += phone.STALE_SECONDS + 1
    assert not phones.offered("phone_battery")


def test_the_most_recent_phone_answers(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    now[0] += 10
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    assert phones.phone_for("phone_battery") == OTHER


def test_a_call_goes_to_the_phone_that_sent_the_message(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    assert phones.here(PHONE, "sid-pixel")
    now[0] += 10
    assert phones.here(OTHER, "sid-tablet")
    # Reported or used later, the other phone doesn't take the Pixel's chat.
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    assert phones.phone_for("phone_battery", "sid-pixel") == PHONE
    assert phones.phone_for("phone_battery", "sid-tablet") == OTHER


def test_the_sending_phone_with_the_tool_off_isnt_passed_over(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", [])
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_location"])
    phones.here(PHONE, "sid-pixel")
    device, refusal = phones.route("phone_location", "sid-pixel")
    assert device is None and "Pixel" in refusal
    assert json.loads(phones.call("phone_location", {}, sid="sid-pixel"))["error"] == refusal


def test_a_turn_no_phone_sent_goes_to_the_first_phone_in_the_users_order(tmp_path):
    now = [1_000_000.0]
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    phones.here(PHONE, "sid-pixel")
    now[0] += 10
    phones.here(OTHER, "sid-tablet")
    # No order: the phone used last. A cron job has no chat connection; Desktop's isn't a phone's.
    assert phones.phone_for("phone_battery") == OTHER
    assert phones.phone_for("phone_battery", "sid-desktop") == OTHER
    assert [p["id"] for p in phones.listing(PHONE)] == [OTHER, PHONE]
    assert phones.order([PHONE, OTHER])
    # A side question just sent from the Tablet has no connection either: it still goes there.
    assert phones.phone_for("phone_battery") == OTHER
    now[0] += phone.JUST_SENT_SECONDS
    assert phones.phone_for("phone_battery") == PHONE
    # The order is kept across reports.
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    assert [p["id"] for p in phones.listing(OTHER)] == [PHONE, OTHER]
    # The first phone with the tool off: the next one that has it.
    phones.report(PHONE, KEY, "Pixel", [])
    assert phones.phone_for("phone_battery") == OTHER


def test_the_list_shows_names_and_switches_but_no_secrets(tmp_path):
    phones = Phones(Store(tmp_path), wake=lambda d, r: None)
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"], push_id=OTHER)
    phones.report(OTHER, OTHER_KEY, "Tablet", [])
    listed = {p["id"]: p for p in phones.listing(PHONE)}
    assert listed[PHONE]["me"] and not listed[OTHER]["me"]
    assert listed[PHONE]["name"] == "Pixel" and listed[PHONE]["tools"] == ["phone_battery"]
    shown = json.dumps(listed)
    assert "key" not in shown and "push" not in shown and phones._hash(KEY) not in shown


def test_a_removed_phone_is_forgotten_and_its_calls_refused(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    phones.here(OTHER, "sid-tablet")
    result = {}

    def call():
        result["r"] = phones.call("phone_battery", {}, sid="sid-tablet")
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not phones.pending(OTHER) and time.time() < deadline:
        time.sleep(0.02)
    assert phones.remove(OTHER)
    thread.join(5)
    assert json.loads(result["r"])["error"] == "The user removed that phone."
    assert [p["id"] for p in phones.listing(PHONE)] == [PHONE]
    assert not phones.verify(OTHER, OTHER_KEY)
    # Its chats now go by the order, and it can come back as new.
    assert phones.phone_for("phone_battery", "sid-tablet") == PHONE
    phones.report(OTHER, "c" * 64, "Tablet", [])
    assert not phones.remove("nope")


def test_a_phone_removed_after_routing_doesnt_leave_the_call_waiting(tmp_path, monkeypatch):
    phones = Phones(Store(tmp_path))
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    # Routed to the Tablet, then removed before the call wrote its request: nothing would answer it.
    monkeypatch.setattr(phones, "route", lambda tool, sid="": (OTHER, None))
    phones.remove(OTHER)
    started = time.time()
    assert json.loads(phones.call("phone_battery", {}))["error"] == "The user removed that phone."
    assert time.time() - started < 1
    assert not list((tmp_path / "phone").glob("*.req"))


def test_bad_connection_ids_are_refused(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    assert not phones.here(PHONE, "")
    assert not phones.here(PHONE, "../x y")
    assert not phones.here(PHONE, "x" * 200)
    assert not phones.order([5])


def test_old_connections_are_forgotten(tmp_path, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(phone, "MAX_HERE", 3)
    phones = Phones(Store(tmp_path), clock=lambda: now[0])
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    for i in range(5):
        now[0] += 1
        phones.here(PHONE, f"sid-{i}")
    assert sorted(phones._here()["sids"]) == ["sid-2", "sid-3", "sid-4"]
    now[0] += phone.HERE_SECONDS + 1
    phones.here(PHONE, "sid-new")
    assert list(phones._here()["sids"]) == ["sid-new"]


def test_a_call_waits_for_the_phone_and_cleans_up(tmp_path):
    woken = []
    phones = Phones(Store(tmp_path), wake=lambda d, r: woken.append((d, r)))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"], push_id=OTHER)
    answer_when_asked(phones, PHONE, result={"level": 80})
    assert json.loads(phones.call("phone_battery", {}, "s1")) == {"level": 80}
    # The nudge goes to the phone's push registration.
    deadline = time.time() + 2
    while not woken and time.time() < deadline:
        time.sleep(0.02)
    assert woken and woken[0][0] == OTHER
    assert list((tmp_path / "phone").glob("*.req.json")) == []
    assert list((tmp_path / "phone").glob("*.res.json")) == []


def test_pending_shows_the_call_to_its_phone_only(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_location"])
    seen = {}

    def peek():
        deadline = time.time() + 5
        while time.time() < deadline:
            waiting = phones.pending(PHONE)
            if waiting:
                seen["mine"], seen["other"] = waiting, phones.pending(OTHER)
                # Another phone can't answer it.
                seen["stolen"] = phones.answer(OTHER, waiting[0]["id"], {"lat": 0}, None)
                phones.answer(PHONE, waiting[0]["id"], None, "Location is off.")
                return
            time.sleep(0.02)
    threading.Thread(target=peek, daemon=True).start()
    result = json.loads(phones.call("phone_location", {"fresh": True}, "chat-7"))
    assert result == {"error": "Location is off."}
    assert seen["other"] == [] and seen["stolen"] is False
    assert seen["mine"][0]["tool"] == "phone_location"
    assert seen["mine"][0]["args"] == {"fresh": True} and seen["mine"][0]["session"] == "chat-7"


def test_no_answer_times_out(tmp_path, monkeypatch):
    schema, _, emoji = phone.TOOLS["phone_battery"]
    monkeypatch.setitem(phone.TOOLS, "phone_battery", (schema, 0.3, emoji))
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    assert "didn't answer" in json.loads(phones.call("phone_battery", {}))["error"]
    assert phones.pending(PHONE) == []


def test_stop_ends_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(Phones, "interrupted", staticmethod(lambda: True))
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_open"])
    started = time.time()
    assert "Stopped" in json.loads(phones.call("phone_open", {"kind": "url", "value": "https://x"}))["error"]
    assert time.time() - started < 5


def test_turning_a_tool_off_refuses_what_waits_for_it(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_battery", "phone_network"])

    def switch_off():
        deadline = time.time() + 5
        while time.time() < deadline and not phones.pending(PHONE):
            time.sleep(0.02)
        phones.report(PHONE, KEY, "Pixel", ["phone_network"])
    threading.Thread(target=switch_off, daemon=True).start()
    assert "turned this tool off" in json.loads(phones.call("phone_battery", {}))["error"]
    assert not phones.offered("phone_battery") and phones.offered("phone_network")


def test_a_chat_that_still_has_a_tool_gets_a_refusal(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", [])
    assert "No phone offers" in json.loads(phones.call("phone_battery", {}))["error"]


def test_an_unknown_request_id_is_refused(tmp_path):
    phones = Phones(Store(tmp_path))
    assert phones.answer(PHONE, "../../identity", {}, None) is False
    assert phones.answer(PHONE, "0" * 32, {}, None) is False


def test_a_late_answer_leaves_nothing_behind(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.root.mkdir(parents=True)
    rid = "1" * 32
    # Written as the tool gave up: the request is already gone by the time the answer lands.
    (phones.root / f"{rid}.req.json").write_text(json.dumps({"id": rid, "device_id": PHONE, "expires": 2**40}))
    real_write = phone._write_private

    def write_then_lose_request(path, data):
        real_write(path, data)
        (phones.root / f"{rid}.req.json").unlink()
    phone._write_private = write_then_lose_request
    try:
        assert phones.answer(PHONE, rid, {"x": 1}, None) is False
    finally:
        phone._write_private = real_write
    assert list(phones.root.glob("*.json")) == []


def test_leftovers_are_swept(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.root.mkdir(parents=True)
    old = time.time() - phone.LEFTOVER_SECONDS - 10
    for name in ("2" * 32 + ".req.json", "3" * 32 + ".res.json", ".tmp-x"):
        path = phones.root / name
        path.write_text("{}")
        os.utime(path, (old, old))
    phones.pending(PHONE)
    assert [p.name for p in phones.root.iterdir()] == []


def test_a_huge_answer_becomes_an_error(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_contacts"])
    answer_when_asked(phones, PHONE, result={"x": "a" * (phone.MAX_RESULT + 10)})
    assert "too long" in json.loads(phones.call("phone_contacts", {"query": "a"}))["error"]


def handler_for(tmp_path, trusted=True):
    phones = Phones(Store(tmp_path))
    return commands.make_handler(Store(tmp_path), "t", lambda: True, trusted=lambda: trusted, phones=phones), phones


def test_phone_commands(tmp_path):
    handle, _ = handler_for(tmp_path)
    reply = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "key": KEY, "name": "Pixel",
                                                           "tools": ["phone_battery", "nope"]})))
    assert reply == {"ok": True, "tools": ["phone_battery"]}
    pending = json.loads(handle("phone pending " + json.dumps({"device_id": PHONE, "key": KEY})))
    assert pending["ok"] is True and pending["requests"] == []
    # The gateway's time, so the phone can tell how long each call has left whatever its own clock says.
    assert abs(pending["now"] - time.time()) < 5
    me = {"device_id": PHONE, "key": KEY}
    assert json.loads(handle("phone here " + json.dumps({**me, "sid": "abc123"}))) == {"ok": True}
    assert json.loads(handle("phone here " + json.dumps({**me, "sid": 5})))["ok"] is False
    listed = json.loads(handle("phone list " + json.dumps(me)))
    assert listed["ok"] and [p["id"] for p in listed["phones"]] == [PHONE]
    assert json.loads(handle("phone order " + json.dumps({**me, "ids": [PHONE]})))["ok"] is True
    assert json.loads(handle("phone order " + json.dumps({**me, "ids": "x"})))["ok"] is False
    assert json.loads(handle("phone remove " + json.dumps({**me, "id": PHONE})))["ok"] is False
    assert json.loads(handle("phone remove " + json.dumps({**me, "id": OTHER})))["ok"] is False
    assert json.loads(handle("phone answer {"))["ok"] is False
    answer = {"device_id": PHONE, "key": KEY, "id": "0" * 32}
    assert json.loads(handle("phone answer " + json.dumps(answer)))["ok"] is False
    assert "phone_battery" in json.loads(handle("info"))["phone_tools"]


def test_phone_commands_need_the_phones_key(tmp_path):
    handle, phones = handler_for(tmp_path)
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    for op in ("pending", "answer", "here", "list", "order", "remove"):
        reply = json.loads(handle(f"phone {op} " + json.dumps({"device_id": PHONE, "key": OTHER_KEY, "id": "0" * 32,
                                                              "sid": "abc", "ids": []})))
        assert reply == {"ok": False, "error": "unknown phone"}
    taken = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "key": OTHER_KEY, "tools": []})))
    assert taken["ok"] is False and phones.offered("phone_battery")


def test_phone_commands_refuse_untrusted_surfaces(tmp_path):
    handle, phones = handler_for(tmp_path, trusted=False)
    reply = json.loads(handle("phone tools " + json.dumps({"device_id": PHONE, "key": KEY, "tools": ["phone_battery"]})))
    assert reply["ok"] is False and not phones.offered("phone_battery")


def test_a_big_answer_fits_through_the_command(tmp_path):
    handle, _ = handler_for(tmp_path)
    big = json.dumps({"device_id": PHONE, "key": KEY, "id": "0" * 32, "result": "a" * 20000})
    assert json.loads(handle("phone answer " + big)).get("error") != "too long"
    assert json.loads(handle("register " + "a" * 20000))["error"] == "too long"


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.unloads = []

    def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji=""):
        assert toolset == phone.TOOLSET and schema["name"] == name
        self.tools[name] = (handler, check_fn)

    def on_unload(self, callback):
        self.unloads.append(callback)


@pytest.fixture
def ctx():
    ctx = FakeCtx()
    yield ctx
    for stop in ctx.unloads:
        stop()


def test_tools_are_offered_only_while_a_phone_has_them_on(tmp_path, ctx):
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    assert set(ctx.tools) == set(phone.TOOLS)
    handler, check = ctx.tools["phone_battery"]
    assert check() is False
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    assert check() is True and ctx.tools["phone_network"][1]() is False
    answer_when_asked(phones, PHONE, result={"level": 5})
    # The dispatcher passes context kwargs the handler doesn't use.
    assert json.loads(handler({}, session_id="s", task_id="t", user_task="u")) == {"level": 5}


def test_a_new_chat_is_told_when_to_draw_a_screen_only_while_a_phone_can(tmp_path, ctx, monkeypatch):
    sections = {}

    def register(id, content, **_kw):
        # As Hermes does it: an id once, and text or a callable of the session's info, frozen into each new chat.
        if id in sections:
            raise ValueError(f"system prompt section {id!r} is already registered")
        sections[id] = content

    ctx.register_system_prompt_section = register
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    render = sections[phone.SCREEN_HINT_ID]
    info = {"session_id": "s", "model": "m", "provider": "p", "platform": "", "profile_name": "default", "cwd": ""}
    assert render(info) == ""
    phones.report(PHONE, KEY, "Pixel", [phone.SHOW_UI_TOOL])
    assert phone.SHOW_UI_TOOL in render(info) and "HTML" in render(info)
    # Fits Hermes's cap on one section, with room for others.
    assert len(render(info)) < 1000
    # A chat from a messaging platform is known by its info, before any turn sets the session's context.
    gateway = types.ModuleType("gateway")
    session_context = types.ModuleType("gateway.session_context")
    session_context.NON_MESSAGING_SESSION_SURFACES = frozenset({"cli", "tui", "api_server"})
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    assert render({**info, "platform": "telegram"}) == ""
    assert render({**info, "platform": "tui"}) == phone.SCREEN_HINT
    monkeypatch.setattr(phone, "on_messaging_platform", lambda: True)
    assert render(info) == ""
    # Registered again in the same process (a reinstall), it's refused, and the tools still are there.
    phone.register_tools(ctx, phones)
    assert phone.SHOW_UI_TOOL in ctx.tools


def test_an_older_hermes_without_prompt_sections_still_gets_the_tools(tmp_path, ctx):
    phone.register_tools(ctx, Phones(Store(tmp_path)))
    assert phone.SHOW_UI_TOOL in ctx.tools


def test_the_handler_routes_by_the_turns_chat_connection(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr(phone, "turn_connection", lambda: "sid-pixel")
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    phones.report(PHONE, KEY, "Pixel", ["phone_battery"])
    phones.report(OTHER, OTHER_KEY, "Tablet", ["phone_battery"])
    phones.here(PHONE, "sid-pixel")
    answer_when_asked(phones, PHONE, result={"level": 7})
    assert json.loads(ctx.tools["phone_battery"][0]({})) == {"level": 7}


def test_messaging_platforms_get_no_phone_tools(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr(phone, "on_messaging_platform", lambda: True)
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    phones.report(PHONE, KEY, "Pixel", ["phone_location"])
    assert "messaging platform" in json.loads(ctx.tools["phone_location"][0]({}))["error"]
    assert phones.pending(PHONE) == []


def test_another_process_changing_switches_refreshes_the_tool_list(tmp_path, ctx, monkeypatch):
    refreshed = []
    monkeypatch.setattr(phone, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(phone, "_refresh_tool_list", lambda: refreshed.append(1))
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", [])
    phone.register_tools(ctx, phones)
    refreshed.clear()
    time.sleep(0.2)
    (phones.root / "phones.json").write_text("{}")
    deadline = time.time() + 3
    while not refreshed and time.time() < deadline:
        time.sleep(0.02)
    assert refreshed


def test_every_schema_is_a_closed_object():
    for name, (schema, timeout, _) in phone.TOOLS.items():
        assert schema["name"] == name and name.startswith("phone_")
        assert schema["parameters"]["additionalProperties"] is False
        assert timeout in (phone.READ_TIMEOUT, phone.CONFIRM_TIMEOUT, phone.CALL_TIMEOUT, phone.SCREEN_TIMEOUT)


def test_a_screen_takes_a2ui_messages_and_waits_for_a_tap():
    schema, timeout, _ = phone.TOOLS["phone_show_ui"]
    params = schema["parameters"]
    assert set(params["properties"]) == {"messages", "wait"} and params["required"] == ["messages"]
    assert params["properties"]["messages"]["maxItems"] == phone.MAX_SCREEN_MESSAGES
    assert timeout == phone.SCREEN_TIMEOUT
    # What the agent is taught is what Kyllene's catalog draws.
    description = schema["description"]
    assert "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json" in description
    # Kyllene draws pictures and media, and changes a screen in place.
    for word in ("Image", "Video", "AudioPlayer", "List", "Modal", "openUrl", "primaryColor", "deleteSurface"):
        assert word in description
    assert "aren't shown" not in description
    # Updates are for the same task: a new request gets its own screen under the new reply.
    assert "new request" in description and "new request" in phone.SCREEN_HINT
    assert '"version":"v0.9"' in description and '"root"' in description
    # A fixed id would be copied into every call.
    assert "pick-1" not in description


def test_a_call_takes_no_number_only_a_reason():
    schema = phone.TOOLS[phone.CALL_TOOL][0]["parameters"]
    assert set(schema["properties"]) == {"reason", "details"} and schema["required"] == ["reason"]


@pytest.mark.parametrize("status", ["answered", "declined", "no_answer", "busy", "quiet"])
def test_a_call_returns_how_it_ended_with_what_to_do_next(tmp_path, ctx, status):
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    phones.report(PHONE, KEY, "Pixel", [phone.CALL_TOOL])
    seen = {}

    def ring():
        deadline = time.time() + 5
        while time.time() < deadline:
            waiting = phones.pending(PHONE)
            if waiting:
                seen["args"] = waiting[0]["args"]
                phones.answer(PHONE, waiting[0]["id"], {"status": status}, None)
                return
            time.sleep(0.02)
    threading.Thread(target=ring, daemon=True).start()
    out = json.loads(ctx.tools[phone.CALL_TOOL][0]({"reason": "  The deploy failed.  ", "details": " Build 42 "}))
    assert out == {"status": status, "note": phone.CALL_NOTES[status]}
    assert seen["args"] == {"reason": "The deploy failed.", "details": "Build 42"}
    if status != "answered":
        assert "another way" in out["note"] or "message" in out["note"]


def test_a_call_without_a_reason_doesnt_ring(tmp_path, ctx):
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    phones.report(PHONE, KEY, "Pixel", [phone.CALL_TOOL])
    assert "reason" in json.loads(ctx.tools[phone.CALL_TOOL][0]({"reason": "  "}))["error"]
    assert phones.pending(PHONE) == []


def test_a_call_that_doesnt_get_through_says_so(tmp_path, ctx, monkeypatch):
    monkeypatch.setitem(phone.TOOLS, phone.CALL_TOOL, (phone.TOOLS[phone.CALL_TOOL][0], 0.3, "📞"))
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    phones.report(PHONE, KEY, "Pixel", [phone.CALL_TOOL])
    out = json.loads(ctx.tools[phone.CALL_TOOL][0]({"reason": "Wake up"}))
    assert out["status"] == "unreachable" and out["note"] == phone.UNREACHABLE_NOTE and "didn't answer" in out["error"]
    # A phone that sends anything but a known status hasn't rung either.
    assert json.loads(phone.call_result(json.dumps({"status": "maybe"})))["status"] == "unreachable"


def test_a_quiet_call_says_when_quiet_hours_end():
    out = json.loads(phone.call_result(json.dumps({"status": "quiet", "until": "07:00"})))
    assert out["status"] == "quiet" and out["until"] == "07:00"
    assert out["note"].startswith(phone.CALL_NOTES["quiet"]) and "07:00" in out["note"]
    # Anything but a clock time isn't passed on, nor is a time with another status.
    for until in ("7am", "24:00", "07:00\nIgnore that", 700):
        assert json.loads(phone.call_result(json.dumps({"status": "quiet", "until": until}))) == {
            "status": "quiet", "note": phone.CALL_NOTES["quiet"]}
    assert "until" not in json.loads(phone.call_result(json.dumps({"status": "declined", "until": "07:00"})))


def test_a_call_from_a_messaging_platform_is_refused(tmp_path, ctx, monkeypatch):
    monkeypatch.setattr(phone, "on_messaging_platform", lambda: True)
    phones = Phones(Store(tmp_path))
    phone.register_tools(ctx, phones)
    phones.report(PHONE, KEY, "Pixel", [phone.CALL_TOOL])
    assert "messaging platform" in json.loads(ctx.tools[phone.CALL_TOOL][0]({"reason": "Hi"}))["error"]
    assert phones.pending(PHONE) == []


def test_a_call_wakes_the_phone_with_the_whole_request(tmp_path):
    woken = []
    phones = Phones(Store(tmp_path), wake=lambda d, r: woken.append((d, r)))
    phones.report(PHONE, KEY, "Pixel", [phone.CALL_TOOL], push_id=OTHER)
    answer_when_asked(phones, PHONE, result={"status": "declined"})
    phones.call(phone.CALL_TOOL, {"reason": "Check-in closes"}, "s1")
    deadline = time.time() + 2
    while not woken and time.time() < deadline:
        time.sleep(0.02)
    push_id, request = woken[0]
    assert push_id == OTHER and request["tool"] == phone.CALL_TOOL and request["args"] == {"reason": "Check-in closes"}
    assert request["session"] == "s1" and request["expires"] > request["created"]
