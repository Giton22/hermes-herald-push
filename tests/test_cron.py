import json
import os
import sys
import time
from datetime import datetime

import pytest

from herald_push import commands, crypto, cron, push, watcher
from herald_push.store import Device, Store


def phone(device_id="0b6f2c1e-1111-4222-8333-944445555666"):
    enc, sig = crypto.generate_key(), crypto.generate_key()
    data = {
        "device_id": device_id, "name": "Pixel", "server": "https://ntfy.sh",
        "push_topic": crypto.new_topic(), "reply_topic": crypto.new_topic(),
        "enc_pub": crypto.encode_public(enc.public_key()), "sig_pub": crypto.encode_public(sig.public_key()),
    }
    return data, enc


def agent_run(name="morning-briefing", job="006998ffc458", response="# Morning\n\nAll good."):
    return (f"# Cron Job: {name}\n\n**Job ID:** {job}\n**Run Time:** 2026-10-12 08:02:06\n**Schedule:** 0 8 * * *\n\n"
            f"## Prompt\n\nDo the briefing.\n\n## Script Output\n\n## Response\n\n{response}\n")


def write_run(home, job, stem, doc, age=60):
    folder = home / "cron" / "output" / job
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.md"
    path.write_text(doc, encoding="utf-8")
    then = time.time() - age
    os.utime(path, (then, then))
    return path


# ── reading a run ────────────────────────────────────────────────────────────

def test_an_agent_runs_reply_is_its_text():
    assert cron.parse_run(agent_run()) == ("morning-briefing", cron.OK, "# Morning\n\nAll good.")


def test_a_reply_that_stayed_silent_is_not_a_run():
    assert cron.parse_run(agent_run(response="[SILENT]")) is None
    assert cron.parse_run(agent_run(response="")) is None


def test_a_failed_run_says_why_and_drops_the_failed_tag_from_its_name():
    doc = ("# Cron Job: Email daily digest (FAILED)\n\n**Job ID:** 53dce36a473d\n**Run Time:** 2026-09-16 21:06:33\n\n"
           "## Prompt\n\nDigest.\n\n## Error\n\n```\nRuntimeError: no reply\n```\n")
    assert cron.parse_run(doc) == ("Email daily digest", cron.FAILED, "RuntimeError: no reply")


def test_a_scripts_output_is_its_text_and_a_silent_script_is_nothing():
    header = "# Cron Job: tool-health-check\n\n**Job ID:** f9ec66b227a6\n**Run Time:** 2026-10-05 08:00:54\n**Mode:** no_agent (script)\n"
    assert cron.parse_run(header + "\n---\n\n2 issues found\n\n---\n") == ("tool-health-check", cron.OK, "2 issues found")
    assert cron.parse_run(header + "**Status:** silent (empty output)\n") is None
    assert cron.parse_run(header + "**Status:** silent (wakeAgent=false)\n") is None
    assert cron.parse_run(header + "**Status:** script failed\n\nexit 2\n")[1:] == (cron.FAILED, "exit 2")


def test_a_gated_run_that_never_woke_the_agent_is_nothing():
    doc = "# Cron Job: Email trainer\n\n**Job ID:** b1365b96ae90\n**Run Time:** 2026-10-02 11:00:51\n\nScript gate returned `wakeAgent=false` — agent skipped."
    assert cron.parse_run(doc) is None


def test_the_schedulers_last_ditch_error_is_a_failure():
    assert cron.parse_run("# Cron Job: x\n\nError: boom\n") == ("x", cron.FAILED, "boom")


# ── what a phone follows ─────────────────────────────────────────────────────

def test_follow_decides_what_is_sent_and_whether_it_rings():
    follow = cron.Follow({"a": "notify", "b": "quiet"}, failures=True)
    assert follow.wants("a", cron.OK) is True
    assert follow.wants("b", cron.OK) is False
    assert follow.wants("c", cron.OK) is None
    # Any failure rings when the phone asked for failures, even a job it doesn't follow or keeps quiet.
    assert follow.wants("b", cron.FAILED) is True
    assert follow.wants("c", cron.FAILED) is True
    assert cron.Follow({"c": "quiet"}).wants("c", cron.FAILED) is False


@pytest.mark.parametrize("bad", [
    {"jobs": {"../x": "notify"}}, {"jobs": {"a": "loud"}}, {"jobs": []}, {"failures": "yes"}, {"default": "loud"},
    {"jobs": {f"j{i}": "quiet" for i in range(cron.MAX_JOBS + 1)}},
])
def test_follow_is_validated(bad):
    with pytest.raises(ValueError):
        cron.Follow.parse(bad)


def test_only_a_registered_phone_can_follow(tmp_path):
    store = Store(tmp_path)
    follows = cron.Follows(store)
    with pytest.raises(ValueError):
        follows.set("0b6f2c1e-1111-4222-8333-944445555666", cron.Follow({"a": "notify"}))
    data, _ = phone()
    store.upsert(Device.parse(data))
    follows.set(data["device_id"], cron.Follow({"a": "notify"}, failures=True))
    assert follows.get(data["device_id"]) == cron.Follow({"a": "notify"}, True)
    if sys.platform != "win32":  # POSIX modes
        assert oct(os.stat(follows.path).st_mode & 0o777) == "0o600"


def test_cron_command_follows_and_reports(tmp_path):
    store = Store(tmp_path)
    data, _ = phone()
    store.upsert(Device.parse(data))
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True)
    body = json.dumps({"device_id": data["device_id"], "jobs": {"006998ffc458": "notify"}, "failures": True})
    assert json.loads(handle("cron follow " + body)) == {"ok": True, "jobs": {"006998ffc458": "notify"}, "failures": True, "default": "off"}
    asked = json.loads(handle("cron following " + json.dumps({"device_id": data["device_id"]})))
    assert asked["jobs"] == {"006998ffc458": "notify"}
    assert json.loads(handle("cron follow {\"device_id\": \"nope\"}"))["ok"] is False
    assert json.loads(handle("cron follow not-json"))["ok"] is False


def test_cron_command_is_refused_off_the_dashboard(tmp_path):
    handle = commands.make_handler(Store(tmp_path), "t", lambda: True, trusted=lambda: False)
    assert json.loads(handle("cron following {}"))["ok"] is False


# ── finding new runs ─────────────────────────────────────────────────────────

def test_a_jobs_history_is_not_news_but_its_next_run_is(tmp_path):
    home = tmp_path / "home"
    write_run(home, "j1", "2026-10-11_08-02-07", agent_run(response="old"))
    marks = {}
    found, changed = cron.CronWatcher().poll([("default", home)], marks)
    assert found == [] and changed and marks == {"cron:default:j1": "2026-10-11_08-02-07"}

    write_run(home, "j1", "2026-10-12_08-02-07", agent_run(response="new"))
    found, _ = cron.CronWatcher().poll([("default", home)], marks)
    assert [r.text for r in found] == ["new"]
    assert found[0].job_id == "j1" and found[0].run == "2026-10-12_08-02-07" and found[0].name == "morning-briefing"
    assert cron.CronWatcher().poll([("default", home)], marks)[0] == []


def test_a_run_still_being_written_waits_and_a_stale_one_is_skipped(tmp_path):
    home = tmp_path / "home"
    write_run(home, "j1", "2026-10-10_08-00-00", agent_run())
    marks = {}
    cron.CronWatcher().poll([("default", home)], marks)
    write_run(home, "j1", "2026-10-11_08-00-00", agent_run(response="stale"), age=cron.STALE_SECONDS + 60)
    write_run(home, "j1", "2026-10-12_08-00-00", agent_run(response="fresh"), age=0)
    found, _ = cron.CronWatcher().poll([("default", home)], marks)
    assert found == [] and marks["cron:default:j1"] == "2026-10-11_08-00-00"
    found, _ = cron.CronWatcher(now=lambda: time.time() + 5).poll([("default", home)], marks)
    assert [r.text for r in found] == ["fresh"]


def test_runs_are_found_in_every_profile_and_odd_names_are_ignored(tmp_path):
    root = tmp_path / "root"
    write_run(root, "j1", "2026-10-11_08-00-00", agent_run())
    write_run(root / "profiles" / "bot", "j2", "2026-10-11_08-00-00", agent_run())
    (root / "cron" / "output" / "j1" / "notes.md").write_text("x")
    marks = {}
    cron.CronWatcher().poll(watcher.profile_homes(root), marks)
    assert set(marks) == {"cron:default:j1", "cron:bot:j2"}


# ── end to end ───────────────────────────────────────────────────────────────

def test_the_watcher_pushes_a_followed_run_that_opens_on_the_phone(tmp_path, monkeypatch):
    root = tmp_path / "root"
    store = Store(root / "plugin-data" / "herald-push")
    data, enc = phone()
    other, _ = phone("1b6f2c1e-1111-4222-8333-944445555666")
    store.upsert(Device.parse(data))
    store.upsert(Device.parse(other))
    cron.Follows(store).set(data["device_id"], cron.Follow({"j1": "notify"}))

    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append((device, payload)) or True)
    w = watcher.Watcher(root, store)
    write_run(root, "j1", "2026-10-11_08-00-00", agent_run())
    assert w.poll_cron(store.devices()) == 0
    write_run(root, "j1", "2026-10-12_08-00-00", agent_run(response="Rain later. " + "x" * 3000))
    assert w.poll_cron(store.devices()) == 1

    device, payload = sent[0]
    assert device.device_id == data["device_id"]
    assert payload["type"] == "cron_run" and payload["job"] == "j1" and payload["run"] == "2026-10-12_08-00-00"
    assert payload["label"] == "morning-briefing" and payload["status"] == "ok" and payload["notify"] is True
    assert payload["text"].startswith("Rain later.") and payload["text"].endswith("…")

    # Marks survive a restart: a new watcher doesn't send the same run again.
    assert watcher.Watcher(root, store).poll_cron(store.devices()) == 0

    _, _, sig = store.identity()
    body = push.envelope_for(device, payload, sig)
    assert len(body) <= push.MAX_BODY
    opened = crypto.open_envelope(crypto.Envelope.from_json(body), device.push_topic, crypto.G2P, enc,
                                  crypto.decode_public(store.public_identity()["sig_pub"]))
    assert json.loads(opened)["text"] == payload["text"]


# ── the Inbox list and one run in full ───────────────────────────────────────

def test_inbox_lists_followed_runs_and_failures_newest_first(tmp_path):
    root = tmp_path / "root"
    write_run(root, "brief", "2026-10-11_08-00-00", agent_run(response="Monday"), age=7200)
    write_run(root, "brief", "2026-10-12_08-00-00", agent_run(response="Tuesday " + "x" * 2000), age=3600)
    write_run(root, "brief", "2026-10-13_08-00-00", agent_run(response="[SILENT]"), age=60)
    write_run(root, "noise", "2026-10-12_09-00-00", agent_run(name="noise", response="hourly"), age=1800)
    failed = "# Cron Job: noise (FAILED)\n\n**Job ID:** noise\n\n## Error\n\nboom\n"
    write_run(root, "noise", "2026-10-12_10-00-00", failed, age=900)
    homes = list(watcher.profile_homes(root))

    runs = cron.inbox(homes, cron.Follow({"brief": "quiet"}))
    assert [r["text"][:7] for r in runs] == ["Tuesday", "Monday"]
    assert len(runs[0]["text"]) == cron.LIST_TEXT and runs[0]["text"].endswith("…")

    with_failures = cron.inbox(homes, cron.Follow({"brief": "quiet"}, failures=True))
    assert [(r["job"], r["status"]) for r in with_failures] == [("noise", "failed"), ("brief", "ok"), ("brief", "ok")]
    assert with_failures[0]["label"] == "noise"

    older = cron.inbox(homes, cron.Follow({"brief": "quiet"}), before=runs[0]["at"])
    assert [r["run"] for r in older] == ["2026-10-11_08-00-00"]
    assert cron.inbox(homes, cron.Follow({})) == []


def test_read_gives_one_run_in_full_and_refuses_odd_paths(tmp_path):
    root = tmp_path / "root"
    write_run(root, "brief", "2026-10-12_08-00-00", agent_run(response="Tuesday " + "x" * 2000))
    homes = list(watcher.profile_homes(root))
    run = cron.read(homes, "default", "brief", "2026-10-12_08-00-00")
    assert run["text"].startswith("Tuesday") and len(run["text"]) > 2000
    assert cron.read(homes, "default", "brief", "../../etc/passwd") is None
    assert cron.read(homes, "default", "..", "2026-10-12_08-00-00") is None
    assert cron.read(homes, "nobody", "brief", "2026-10-12_08-00-00") is None


def test_inbox_and_read_commands(tmp_path):
    root = tmp_path / "root"
    store = Store(root / "plugin-data" / "herald-push")
    data, _ = phone()
    store.upsert(Device.parse(data))
    write_run(root, "brief", "2026-10-12_08-00-00", agent_run(response="Tuesday"))
    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True, root=root)
    me = {"device_id": data["device_id"]}
    assert json.loads(handle("cron inbox " + json.dumps(me)))["runs"] == []
    handle("cron follow " + json.dumps({**me, "jobs": {"brief": "notify"}}))
    runs = json.loads(handle("cron inbox " + json.dumps(me)))["runs"]
    assert [r["text"] for r in runs] == ["Tuesday"]
    one = json.loads(handle("cron read " + json.dumps({**me, "bot": "default", "job": "brief", "run": runs[0]["run"]})))
    assert one["ok"] and one["run"]["text"] == "Tuesday"
    assert json.loads(handle("cron read " + json.dumps({**me, "bot": "default", "job": "brief", "run": "nope"})))["ok"] is False
    assert json.loads(handle("cron inbox " + json.dumps({**me, "limit": "x"})))["ok"] is False


# ── every job by default ─────────────────────────────────────────────────────

def test_a_default_follows_every_job_but_the_ones_switched_off():
    follow = cron.Follow.parse({"default": "quiet", "jobs": {"noisy": "off", "brief": "notify", "pulse": "quiet"}})
    # "pulse" says what the default says, so it isn't kept.
    assert follow.jobs == {"noisy": "off", "brief": "notify"}
    assert follow.wants("brand-new-job", cron.OK) is False
    assert follow.wants("brief", cron.OK) is True
    assert follow.wants("noisy", cron.OK) is None
    assert cron.Follow.parse({"jobs": {"a": "off"}}).wants("a", cron.OK) is None
    # Without a default nothing changes from before: only the listed jobs.
    assert cron.Follow.parse({"jobs": {"a": "quiet"}}).wants("b", cron.OK) is None


def test_a_cron_json_from_before_defaults_reads_as_off(tmp_path):
    store = Store(tmp_path)
    data, _ = phone()
    store.upsert(Device.parse(data))
    (tmp_path / "cron.json").write_text(json.dumps({data["device_id"]: {"jobs": {"a": "quiet"}, "failures": False}}))
    follow = cron.Follows(store).get(data["device_id"])
    assert follow.default == "off" and follow.follows("a") and not follow.follows("b")


def test_the_inbox_lists_every_job_when_following_all(tmp_path):
    root = tmp_path / "root"
    write_run(root, "brief", "2026-10-12_08-00-00", agent_run(response="Morning"))
    write_run(root, "pulse", "2026-10-12_13-00-00", agent_run(name="pulse", response="Tickets"))
    write_run(root, "noisy", "2026-10-12_14-00-00", agent_run(name="noisy", response="Spam"))
    write_run(root, "quiet", "2026-10-12_15-00-00", agent_run(name="quiet", response="[SILENT]"))
    homes = list(watcher.profile_homes(root))
    runs = cron.inbox(homes, cron.Follow.parse({"default": "quiet", "jobs": {"noisy": "off"}}))
    # The silent job brings nothing, and the one switched off stays out.
    assert sorted(r["job"] for r in runs) == ["brief", "pulse"]


# ── what the scheduler writes, read as it reads it ───────────────────────────

def framed_run(prompt="Do the briefing.", response="# Morning\n\nAll good.", name="morning-briefing"):
    """A run as current Hermes writes it: the prompt's and the reply's lengths stamped above each."""
    return (f"# Cron Job: {name}\n\n**Job ID:** 006998ffc458\n**Run Time:** 2026-10-12 08:02:06\n**Schedule:** 0 8 * * *\n\n"
            f"**Prompt Characters:** {len(prompt)}\n## Prompt\n\n{prompt}\n\n"
            f"**Response Characters:** {len(response)}\n## Response\n\n{response}\n")


def test_a_reply_with_its_own_error_heading_is_still_a_success():
    reply = "# Log digest\n\n## Error\n\n3 timeouts overnight.\n\n## Fine\n\nRest ok."
    for doc in (agent_run(response=reply), framed_run(response=reply)):
        assert cron.parse_run(doc) == ("morning-briefing", cron.OK, reply)


def test_headings_quoted_in_the_prompt_are_not_the_schedulers():
    prompt = "Answer in this shape:\n\n## Response\n\nyour text\n\n**Status:** silent\n\n## Error\n\nnever"
    assert cron.parse_run(framed_run(prompt=prompt, response="Real answer")) == ("morning-briefing", cron.OK, "Real answer")
    # Written before frames, the last Response heading is the scheduler's.
    legacy = agent_run(response="Real answer").replace("Do the briefing.", prompt)
    assert cron.parse_run(legacy)[1:] == (cron.OK, "Real answer")


def test_the_agents_failure_marker_fails_the_run():
    assert cron.parse_run(framed_run(response="[CRON_FAILURE]\nThe child failed: timeout.")) == (
        "morning-briefing", cron.FAILED, "The child failed: timeout.")
    assert cron.parse_run(agent_run(response="[CRON_FAILURE]"))[1] == cron.FAILED
    # Only the exact first line, as the scheduler reads it: a report quoting it is a success.
    assert cron.parse_run(framed_run(response="Saw [CRON_FAILURE] in the log"))[1] == cron.OK
    assert cron.parse_run(framed_run(response="Report\n[CRON_FAILURE]"))[1] == cron.OK


def test_a_run_the_scanner_blocked_is_a_failure():
    doc = ("# Cron Job: x\n\n**Job ID:** j\n**Run Time:** 2026-10-12 08:00:00\n**Status:** BLOCKED\n\n"
           "The assembled prompt tripped the cron injection scanner.")
    assert cron.parse_run(doc) == ("x", cron.FAILED, "The assembled prompt tripped the cron injection scanner.")


def test_a_scripts_colours_are_dropped(tmp_path):
    header = "# Cron Job: health\n\n**Job ID:** f9ec66b227a6\n**Mode:** no_agent (script)\n"
    path = write_run(tmp_path, "health", "2026-10-12_08-00-00", header + "\n---\n\n\x1b[32mOK\x1b[0m disk\n\n---\n")
    assert cron.read_run("default", "health", path).text == "OK disk"


def test_a_run_is_dated_by_its_name_not_its_file(tmp_path):
    path = write_run(tmp_path, "j1", "2026-10-01_08-00-00", agent_run(), age=0)  # a copy or a restored backup
    assert cron.read_run("default", "j1", path).at == datetime(2026, 10, 1, 8, 0, 0).timestamp()


def test_a_job_made_after_the_watcher_started_has_its_first_run_found(tmp_path):
    home = tmp_path / "home"
    write_run(home, "old", "2026-10-11_08-00-00", agent_run(response="old history"))
    marks = {}
    w = cron.CronWatcher()
    assert w.poll([("default", home)], marks)[0] == []
    write_run(home, "newjob", "2026-10-12_09-00-00", agent_run(name="new", response="first report"))
    assert [r.text for r in w.poll([("default", home)], marks)[0]] == ["first report"]


def test_after_a_restart_a_new_jobs_fresh_run_is_found_and_its_old_history_is_not(tmp_path):
    home = tmp_path / "home"
    write_run(home, "old", "2026-10-11_08-00-00", agent_run())
    marks = {}
    cron.CronWatcher().poll([("default", home)], marks)
    # Made while the gateway was down: one stale run, one fresh.
    write_run(home, "newjob", "2026-10-12_07-00-00", agent_run(response="stale"), age=cron.STALE_SECONDS + 60)
    write_run(home, "newjob", "2026-10-12_09-00-00", agent_run(response="fresh"))
    assert [r.text for r in cron.CronWatcher().poll([("default", home)], marks)[0]] == ["fresh"]


def test_a_push_too_large_once_sealed_is_cut_to_fit_and_the_pass_goes_on(tmp_path, monkeypatch):
    root = tmp_path / "root"
    store = Store(root / "plugin-data" / "herald-push")
    data, enc = phone()
    store.upsert(Device.parse(data))
    cron.Follows(store).set(data["device_id"], cron.Follow({}, default="notify"))
    posts = []

    class Client:
        def post(self, url, content=None, headers=None):
            posts.append(content.decode("utf-8"))
            return type("Ok", (), {"status_code": 200})()

    w = watcher.Watcher(root, store)
    write_run(root, "j1", "2026-10-11_08-00-00", agent_run())
    w.poll_cron(store.devices(), Client())
    # Quotes and tabs: JSON escaping doubles the clipped text, past ntfy's limit once sealed.
    write_run(root, "j1", "2026-10-12_08-00-00", agent_run(response='"\t' * 1500 + "end"))
    assert w.poll_cron(store.devices(), Client()) == 1
    assert len(posts[0]) <= push.MAX_BODY
    opened = json.loads(crypto.open_envelope(crypto.Envelope.from_json(posts[0]), data["push_topic"], crypto.G2P, enc,
                                             crypto.decode_public(store.public_identity()["sig_pub"])))
    assert opened["text"].startswith('"\t"') and opened["text"].endswith("…")
    assert len(opened["text"]) < len(push.clip('"\t' * 1500))


def test_a_page_keeps_every_run_of_its_last_second(tmp_path):
    home = tmp_path / "home"
    for job in ("a", "b", "c"):  # fired in the same tick
        write_run(home, job, "2026-10-10_08-00-00", agent_run(response=job))
    follow = cron.Follow({}, default="quiet")
    first = cron.inbox([("default", home)], follow, limit=2)
    assert sorted(r["job"] for r in first) == ["a", "b", "c"]
    assert cron.inbox([("default", home)], follow, limit=2, before=first[-1]["at"]) == []


def test_silent_runs_dont_hide_older_ones(tmp_path):
    home = tmp_path / "home"
    write_run(home, "real", "2026-09-01_08-00-00", agent_run(response="the report"))
    for i in range(450):
        write_run(home, "pulse", f"2026-09-{2 + i // 24:02d}_{i % 24:02d}-00-00", agent_run(response="[SILENT]"))
    assert [r["text"] for r in cron.inbox([("default", home)], cron.Follow({}, default="quiet"))] == ["the report"]


def test_each_profile_dates_its_runs_on_its_own_clock(tmp_path, monkeypatch):
    from datetime import timedelta, timezone
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.setattr(cron, "ZoneInfo", lambda name: timezone(timedelta(hours=9)) if name == "Asia/Tokyo" else None)
    root = tmp_path / "root"
    bot = root / "profiles" / "bot"
    write_run(root, "j1", "2026-10-12_08-00-00", agent_run(response="here"))
    write_run(bot, "j2", "2026-10-12_08-00-00", agent_run(response="tokyo"))
    (bot / "config.yaml").write_text("timezone: Asia/Tokyo\n")
    runs = {r["text"]: r["at"] for r in cron.inbox(list(watcher.profile_homes(root)), cron.Follow({}, default="quiet"))}
    assert runs["here"] == int(datetime(2026, 10, 12, 8).timestamp())
    assert runs["tokyo"] == int(datetime(2026, 10, 12, 8, tzinfo=timezone(timedelta(hours=9))).timestamp())


def test_runs_read_as_the_scheduler_reads_them():
    # Its own tag is in capitals; a job may be named with the word.
    assert cron.parse_run(agent_run(name="Retry (failed)"))[:2] == ("Retry (failed)", cron.OK)
    # The marker counts only as the reply's very first line.
    assert cron.parse_run(framed_run(response="\n[CRON_FAILURE]\nx"))[1] == cron.OK
    # Silence alone on the last line, or opening the reply.
    assert cron.parse_run(framed_run(response="Nothing new.\n[SILENT]")) is None
    assert cron.parse_run(framed_run(response="[SILENT] no changes")) is None
    # An agent that said nothing failed.
    assert cron.parse_run(framed_run(response="(No response generated)"))[1] == cron.FAILED
    # A framed run cut short has no reply, whatever its prompt quotes.
    cut = framed_run(prompt="Shape:\n## Response\nquoted", response="real")[:-20]
    assert cron.parse_run(cut) is None
    monitor = "# Cron Job: m\n\n**Job ID:** j\n**Run Time:** 2026-10-12 08:00:00\n**Status:** monitor source failed\n\nHTTP 500\n"
    assert cron.parse_run(monitor) == ("m", cron.FAILED, "HTTP 500")


def test_a_deleted_jobs_mark_goes(tmp_path):
    home = tmp_path / "home"
    path = write_run(home, "j1", "2026-10-11_08-00-00", agent_run())
    marks = {}
    w = cron.CronWatcher()
    w.poll([("default", home)], marks)
    assert "cron:default:j1" in marks
    # A profile missing from one look keeps its marks: its runs aren't sent again when it's back.
    write_run(home, "j1", "2026-10-12_08-00-00", agent_run(response="sent"))
    assert [r.text for r in w.poll([("default", home)], marks)[0]] == ["sent"]
    assert w.poll([], marks) == ([], False)
    assert w.poll([("default", home)], marks)[0] == []
    for run in path.parent.iterdir():
        run.unlink()
    path.parent.rmdir()
    _, changed = w.poll([("default", home)], marks)
    assert changed and marks == {}


def test_a_profiles_zone_is_its_own_and_the_env_speaks_for_default_only(tmp_path, monkeypatch):
    from datetime import timedelta, timezone
    zones = {"Asia/Tokyo": timezone(timedelta(hours=9)), "Europe/Berlin": timezone(timedelta(hours=2))}
    monkeypatch.setattr(cron, "ZoneInfo", lambda name: zones[name])
    monkeypatch.setenv("HERMES_TIMEZONE", "Europe/Berlin")
    (tmp_path / "config.yaml").write_text("timezone: Asia/Tokyo\n")
    assert cron.zone_of("bot", tmp_path) == zones["Asia/Tokyo"]
    assert cron.zone_of("bot", tmp_path / "nothing") is None
    assert cron.zone_of("default", tmp_path / "nothing") == zones["Europe/Berlin"]


def test_paging_reaches_a_busy_jobs_older_runs(tmp_path):
    home = tmp_path / "home"
    for i in range(40):
        write_run(home, "j1", f"2026-09-{1 + i // 24:02d}_{i % 24:02d}-00-00", agent_run(response=f"r{i}"))
    follow = cron.Follow({"j1": "quiet"})
    first = cron.inbox([("default", home)], follow, limit=30)
    older = cron.inbox([("default", home)], follow, limit=30, before=min(r["at"] for r in first))
    assert [r["text"] for r in first + older] == [f"r{i}" for i in reversed(range(40))]


# ── the session each run ran in ──────────────────────────────────────────────

def cron_sessions(home, *rows):
    """Cron sessions in the profile's state.db: (id, started, ended, end reason[, parent]) with datetimes."""
    import sqlite3
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, parent_session_id TEXT,"
                 " started_at REAL NOT NULL, ended_at REAL, end_reason TEXT)")
    for sid, started, ended, reason, *parent in rows:
        conn.execute("INSERT INTO sessions VALUES (?, 'cron', ?, ?, ?, ?)",
                     (sid, parent[0] if parent else None, started.timestamp(), ended and ended.timestamp(), reason))
    conn.commit()
    conn.close()


def at(stamp):
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")


def test_a_run_names_the_session_it_ran_in_everywhere_it_goes(tmp_path, monkeypatch):
    root = tmp_path / "root"
    store = Store(root / "plugin-data" / "herald-push")
    data, _ = phone()
    store.upsert(Device.parse(data))
    cron.Follows(store).set(data["device_id"], cron.Follow({"j1": "notify"}))
    write_run(root, "j1", "2026-10-25_21-01-12", agent_run(response="yesterday"))
    w = watcher.Watcher(root, store)
    w.poll_cron(store.devices())
    cron_sessions(root, ("cron_j1_20261026_210047", at("2026-10-26 21:01:00"), at("2026-10-26 21:01:12"), "cron_complete"))
    write_run(root, "j1", "2026-10-26_21-01-12", agent_run(response="today"))
    sent = []
    monkeypatch.setattr(push, "publish", lambda device, payload, sig, client=None: sent.append(payload) or True)
    assert w.poll_cron(store.devices()) == 1
    assert sent[0]["session"] == "cron_j1_20261026_210047"
    _, _, sig = store.identity()
    assert len(push.envelope_for(store.devices()[0], sent[0], sig)) <= push.MAX_BODY

    handle = commands.make_handler(store, "t", lambda: True, trusted=lambda: True, root=root)
    me = {"device_id": data["device_id"]}
    runs = json.loads(handle("cron inbox " + json.dumps(me)))["runs"]
    assert [(r["text"], r.get("session")) for r in runs] == [("today", "cron_j1_20261026_210047"), ("yesterday", None)]
    assert "session" not in runs[1]
    one = json.loads(handle("cron read " + json.dumps({**me, "bot": "default", "job": "j1", "run": "2026-10-26_21-01-12"})))
    assert one["run"]["session"] == "cron_j1_20261026_210047"


def test_a_compressed_run_names_the_continuation_it_ended_in(tmp_path):
    home = tmp_path / "home"
    write_run(home, "j1", "2026-10-26_09-30-05", agent_run())
    cron_sessions(home,
                  ("cron_j1_20261026_090000", at("2026-10-26 09:00:01"), at("2026-10-26 09:20:00"), "compression"),
                  ("20261026_092000_ab12cd", at("2026-10-26 09:20:00"), at("2026-10-26 09:25:00"), "compression",
                   "cron_j1_20261026_090000"),
                  ("20261026_092500_ef34ab", at("2026-10-26 09:25:00"), at("2026-10-26 09:30:04"), "cron_complete",
                   "20261026_092000_ab12cd"))
    assert cron.read([("default", home)], "default", "j1", "2026-10-26_09-30-05")["session"] == "20261026_092500_ef34ab"


def test_a_run_whose_gate_kept_the_agent_asleep_has_no_session(tmp_path):
    home = tmp_path / "home"
    write_run(home, "gate", "2026-10-23_23-00-32", agent_run(response="woke"))
    write_run(home, "gate", "2026-10-24_11-00-32", agent_run(response="still reported"))
    cron_sessions(home, ("cron_gate_20261023_230019", at("2026-10-23 23:00:19"), at("2026-10-23 23:00:31"), "cron_complete"))
    runs = {r["text"]: r for r in cron.inbox([("default", home)], cron.Follow({}, default="quiet"))}
    assert runs["woke"]["session"] == "cron_gate_20261023_230019"
    assert "session" not in runs["still reported"]
    # Only the newest file kept: the session ended too long before it to be its.
    (home / "cron" / "output" / "gate" / "2026-10-23_23-00-32.md").unlink()
    assert "session" not in cron.read([("default", home)], "default", "gate", "2026-10-24_11-00-32")


def test_two_runs_of_a_job_each_name_their_own_session(tmp_path):
    home = tmp_path / "home"
    write_run(home, "j1", "2026-10-26_08-01-12", agent_run(response="first"))
    write_run(home, "j1", "2026-10-26_09-01-14", agent_run(response="second"))
    cron_sessions(home,
                  ("cron_j1_20261026_090047", at("2026-10-26 09:01:00"), at("2026-10-26 09:01:13"), "cron_complete"),
                  ("cron_j1_20261026_080050", at("2026-10-26 08:01:01"), at("2026-10-26 08:01:11"), "cron_complete"))
    runs = cron.inbox([("default", home)], cron.Follow({"j1": "quiet"}))
    assert [(r["text"], r["session"]) for r in runs] == [("second", "cron_j1_20261026_090047"),
                                                          ("first", "cron_j1_20261026_080050")]


def test_a_job_id_with_odd_characters_takes_only_its_own_sessions(tmp_path):
    home = tmp_path / "home"
    write_run(home, "a_b", "2026-10-26_08-01-12", agent_run(response="mine"))
    write_run(home, "a.b", "2026-10-26_08-01-12", agent_run(response="dot"))
    # Sessions of jobs "a_b_c" and "a_bx" (what LIKE 'cron_a_b_%' would also match), and none of a_b's own.
    cron_sessions(home,
                  ("cron_a_b_c_20261026_080100", at("2026-10-26 08:01:01"), at("2026-10-26 08:01:11"), "cron_complete"),
                  ("cron_a_bx_20261026_080101", at("2026-10-26 08:01:02"), at("2026-10-26 08:01:11"), "cron_complete"),
                  ("cron_axb_20261026_080101", at("2026-10-26 08:01:03"), at("2026-10-26 08:01:11"), "cron_complete"))
    runs = cron.inbox([("default", home)], cron.Follow({}, default="quiet"))
    assert [r.get("session") for r in runs] == [None, None]
    cron_sessions(home, ("cron_a.b_20261026_080101", at("2026-10-26 08:01:01"), at("2026-10-26 08:01:11"), "cron_complete"))
    assert cron.read([("default", home)], "default", "a.b", "2026-10-26_08-01-12")["session"] == "cron_a.b_20261026_080101"
    assert "session" not in cron.read([("default", home)], "default", "a_b", "2026-10-26_08-01-12")


def test_another_profiles_sessions_never_match(tmp_path):
    root = tmp_path / "root"
    bot = root / "profiles" / "bot"
    write_run(root, "j1", "2026-10-26_08-01-12", agent_run(response="here"))
    write_run(bot, "j1", "2026-10-26_08-01-12", agent_run(response="bot"))
    cron_sessions(bot, ("cron_j1_20261026_080100", at("2026-10-26 08:01:01"), at("2026-10-26 08:01:11"), "cron_complete"))
    runs = {r["bot"]: r for r in cron.inbox(list(watcher.profile_homes(root)), cron.Follow({}, default="quiet"))}
    assert runs["bot"]["session"] == "cron_j1_20261026_080100"
    assert "session" not in runs["default"]


def test_matching_walks_the_job_sessions_once_not_once_per_run(tmp_path, monkeypatch):
    home = tmp_path / "home"
    rows, runs = [], []
    for i in range(200):
        start = datetime(2026, 10, 1 + i // 24, i % 24, 0, 1)
        rows.append((f"cron_j1_{start:%Y%m%d_%H%M%S}", start, start.replace(second=30), "cron_complete"))
        runs.append(f"{start:%Y-%m-%d_%H}-00-31")
    cron_sessions(home, *rows)
    walks = []

    class Counted(list):
        def __iter__(self):
            walks.append(1)
            return super().__iter__()

    real = cron._job_sessions
    monkeypatch.setattr(cron, "_job_sessions", lambda conn, job_id: Counted(real(conn, job_id)))
    found = cron.run_sessions(home, "j1", runs)
    assert found == {run: sid for run, (sid, *_) in zip(runs, rows)}
    assert len(walks) <= 1


def test_a_gated_run_never_takes_the_session_of_a_run_that_crashed(tmp_path):
    home = tmp_path / "home"
    # Run A crashed: its session never ended and it saved no output. Run B, next, was gated: no session.
    write_run(home, "j1", "2026-09-15_17-01-19", agent_run(response="gated"))
    cron_sessions(home, ("cron_j1_20260915_165424", at("2026-09-15 16:54:35"), None, None))
    late = at("2026-09-15 17:01:19").timestamp() + 3600
    assert cron.run_sessions(home, "j1", ["2026-09-15_17-01-19"], now=late) == {}
    assert "session" not in cron.read([("default", home)], "default", "j1", "2026-09-15_17-01-19")


def test_a_session_swept_up_as_an_orphan_is_no_runs(tmp_path):
    home = tmp_path / "home"
    write_run(home, "j1", "2026-09-15_17-01-19", agent_run(response="gated"))
    # Hermes' sweep closed the dead session a month on: its end says nothing of when the run finished.
    cron_sessions(home, ("cron_j1_20260915_165424", at("2026-09-15 16:54:35"), at("2026-10-15 21:40:24"),
                         "startup_orphan_reap"))
    assert cron.run_sessions(home, "j1", ["2026-09-15_17-01-19"]) == {}
    # Not even a sweep that happened to land within the minute.
    cron_sessions(home, ("cron_j1_20260916_165424", at("2026-09-16 16:54:35"), at("2026-09-16 17:01:30"),
                         "ws_orphan_reap"))
    assert cron.run_sessions(home, "j1", ["2026-09-15_17-01-19", "2026-09-16_17-01-19"]) == {}


def test_a_session_that_ended_long_after_the_save_is_another_runs(tmp_path):
    home = tmp_path / "home"
    cron_sessions(home, ("cron_j1_20261026_080050", at("2026-10-26 08:00:51"), at("2026-10-26 08:05:00"), "cron_complete"))
    assert cron.run_sessions(home, "j1", ["2026-10-26_08-01-12"]) == {}
    assert cron.run_sessions(home, "j1", ["2026-10-26_08-04-30"]) == {"2026-10-26_08-04-30": "cron_j1_20261026_080050"}


def test_a_session_not_ended_yet_is_a_fresh_runs_own(tmp_path):
    home = tmp_path / "home"
    cron_sessions(home, ("cron_j1_20261026_080050", at("2026-10-26 08:00:51"), None, None))
    saved = at("2026-10-26 08:01:12").timestamp()
    # The agent may still be finishing just after its output is saved.
    assert cron.run_sessions(home, "j1", ["2026-10-26_08-01-12"], now=saved + 30) == {
        "2026-10-26_08-01-12": "cron_j1_20261026_080050"}
    assert cron.run_sessions(home, "j1", ["2026-10-26_08-01-12"], now=saved + cron.OPEN_SECONDS + 1) == {}


def test_a_compression_continuation_swept_up_as_an_orphan_is_no_runs(tmp_path):
    home = tmp_path / "home"
    cron_sessions(home,
                  ("cron_j1_20261026_090000", at("2026-10-26 09:00:01"), at("2026-10-26 09:20:00"), "compression"),
                  ("20261026_092000_ab12cd", at("2026-10-26 09:20:00"), at("2026-10-26 09:30:00"), "orphaned_compression",
                   "cron_j1_20261026_090000"))
    assert cron.run_sessions(home, "j1", ["2026-10-26_09-30-05"]) == {}
