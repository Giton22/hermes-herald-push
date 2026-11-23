"""A screen is checked against the Basic Catalog before it leaves the gateway.

The app renders A2UI v0.9 with ``androidx.a2ui`` and the Basic Catalog, and used to get whatever the model wrote.
These hold the check to the catalog: a screen the phone can draw passes, one it can't says why in words the agent
can act on, and nothing reaches a phone in between.
"""

import json
import re
import threading
import time

import pytest

from herald_push import a2ui
from herald_push.phone import Phones
from herald_push.store import Store

PHONE = "0b6f2c1e-1111-4222-8333-944445555666"
KEY = "a" * 64

CATALOG = a2ui.CATALOG_ID


def create(surface="pick", catalog=CATALOG, **extra):
    return {"version": "v0.9", "createSurface": {"surfaceId": surface, "catalogId": catalog, **extra}}


def components(*parts, surface="pick"):
    return {"version": "v0.9", "updateComponents": {"surfaceId": surface, "components": list(parts)}}


def data(value, surface="pick", **extra):
    return {"version": "v0.9", "updateDataModel": {"surfaceId": surface, "value": value, **extra}}


def text(id="root", value="Pick one", **extra):
    return {"id": id, "component": "Text", "text": value, **extra}


def screen(*messages):
    return list(messages)


def test_a_screen_the_app_draws_passes():
    assert a2ui.check(screen(
        create(),
        components(
            {"id": "root", "component": "Column", "children": ["title", "go"]},
            text("title"),
            {"id": "go", "component": "Button", "child": "title",
             "action": {"event": {"name": "pick", "context": {"who": {"path": "/who"}}}}},
        ),
        data({"who": "june"}),
    )) == []


def test_an_update_to_a_screen_already_on_the_phone_passes():
    # Only the messages this call sends are visible here; the rest of that screen is on the phone.
    assert a2ui.check(screen(components(text("title", "Changed")))) == []


def test_a_component_the_catalog_doesnt_know_is_named_with_the_ones_it_does():
    problems = a2ui.check(screen(create(), components(
        {"id": "root", "component": "Column", "children": ["typo"]},
        {"id": "typo", "component": "Colum", "children": []},
    )))
    assert len(problems) == 1
    assert "'Colum'" in problems[0] and "Button" in problems[0] and "TextField" in problems[0]


def test_the_catalog_is_listed_once_for_several_unknown_components():
    problems = a2ui.check(screen(create(), components(
        {"id": "root", "component": "Nope"},
        {"id": "other", "component": "AlsoNope"},
    )))
    assert len(problems) == 2
    assert "Use one of" in problems[0] and "Use one of" not in problems[1]


def test_a_missing_property_is_reported_against_its_component():
    problems = a2ui.check(screen(create(), components(
        {"id": "root", "component": "Card"},  # Card needs a child
    )))
    assert problems and "'root'" in problems[0] and "child" in problems[0]


def test_a_property_the_catalog_doesnt_define_is_reported():
    problems = a2ui.check(screen(create(), components(text("root", "Hi", colr="red"))))
    assert problems and "colr" in problems[0]


def test_a_screen_without_a_root_says_so():
    problems = a2ui.check(screen(create(), components(text("only"))))
    assert problems and "root" in problems[0]


def test_a_component_nothing_points_at_says_so():
    problems = a2ui.check(screen(create(), components(
        {"id": "root", "component": "Column", "children": ["title"]},
        text("title"),
        text("stray", "nobody names me"),
    )))
    assert problems and "stray" in problems[0]


def test_another_catalog_is_refused():
    problems = a2ui.check(screen(create(catalog="https://example.com/catalog.json"), components(text())))
    assert problems and "catalogId" in problems[0]


def test_a_message_that_isnt_one_kind_of_message_is_refused():
    problems = a2ui.check(screen({"version": "v0.9", "createSurface": {"surfaceId": "pick"},
                                 "deleteSurface": {"surfaceId": "pick"}}))
    assert problems and "exactly one of" in problems[0]


def test_a_data_update_may_leave_out_its_path():
    # The specification's own examples do: no path means the root.
    assert a2ui.check(screen(create(), components(text()), data({"name": "june"}))) == []


def test_a_data_update_needs_a_value():
    problems = a2ui.check(screen(create(), components(text()),
                                 {"version": "v0.9", "updateDataModel": {"surfaceId": "pick"}}))
    assert problems and "value" in problems[0]


def test_a_call_is_one_screen_not_a_stream():
    problems = a2ui.check([create()] * (a2ui.MAX_MESSAGES + 1))
    assert problems and str(a2ui.MAX_MESSAGES) in problems[0]


def test_nothing_at_all_is_refused():
    assert a2ui.check([]) and a2ui.check("not a list") and a2ui.check(None)


def test_a_component_name_that_isnt_a_string_is_a_problem_not_a_crash():
    problems = a2ui.check(screen(create(), components({"id": "root", "component": {"name": "Text"}})))
    assert problems and "'root'" in problems[0]


def test_a_check_rule_written_the_wrong_way_is_reported():
    # The catalog's checks take condition and message; call/args are the shape of a function call.
    problems = a2ui.check(screen(create(), components(
        {"id": "root", "component": "TextField", "label": "Name", "checks": [{"call": "required"}]},
    )))
    assert problems and "condition" in problems[0]


def test_the_tool_schema_takes_its_limit_from_the_check():
    from herald_push import phone

    assert phone.MAX_SCREEN_MESSAGES == a2ui.MAX_MESSAGES


def test_the_description_names_exactly_the_catalog_components():
    """The list the model reads is the catalog's, not a hand-kept copy that drifts."""
    from herald_push import phone, screen_catalog

    # The generated line is embedded whole — no hand-edited copy of it in the prose.
    assert screen_catalog.COMPONENTS in phone._SCREEN
    for name in a2ui.component_names():
        assert name in phone._SCREEN


def test_the_screen_text_is_generated_from_the_catalog():
    """Re-vendoring the catalog without rebuilding fails here, not silently in a phone call."""
    from herald_push import screen_catalog, screen_catalog_build

    assert screen_catalog.COMPONENTS == screen_catalog_build.components_line()
    page = (a2ui._DATA / "screen_reference.md").read_text(encoding="utf-8")
    assert page == screen_catalog_build.reference()


def test_the_reference_page_covers_every_component():
    page = (a2ui._DATA / "screen_reference.md").read_text(encoding="utf-8")
    for name in a2ui.component_names():
        assert f"## {name}" in page
    # Every property of one component, with the catalog's own words.
    assert "**variant**" in page and "A hint for the base text style." in page


def test_the_description_stays_small_enough_to_ship():
    """It rides on every request while a phone offers the tool; growth has to be a decision."""
    from herald_push import phone

    assert len(phone._SCREEN) < 4600


def test_the_description_points_at_a_reference_page_that_exists():
    """A pointer the agent can act on: an absolute path to the generated page, not a name it has to hunt for."""
    from herald_push import phone

    assert phone.SCREEN_REFERENCE.is_absolute()
    assert phone.SCREEN_REFERENCE.is_file()
    assert str(phone.SCREEN_REFERENCE) in phone._SCREEN


def test_a_screen_the_catalog_doesnt_know_never_reaches_the_phone(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_show_ui"])
    out = json.loads(phones.call("phone_show_ui", {"messages": screen(
        create(),
        components({"id": "root", "component": "Colum", "children": []}),
    )}))
    assert "Colum" in out["error"]
    # Nothing was queued for the phone, so nothing waits on it either.
    assert phones.pending(PHONE) == []


# ── Express: the compact form the gateway compiles before anything leaves ────────────────────────────────────────


def test_an_express_block_compiles_to_the_messages_the_app_draws():
    pytest.importorskip("a2ui.core.catalog")
    messages = a2ui.compile_express('<a2ui>\nroot = Column([Text("hi"), Text("there")])\n</a2ui>', "pick-1")
    assert messages[0]["version"] == "v0.9"
    assert messages[0]["createSurface"] == {"surfaceId": "pick-1", "catalogId": a2ui.CATALOG_ID}
    components = messages[1]["updateComponents"]["components"]
    assert messages[1]["updateComponents"]["surfaceId"] == "pick-1"
    assert components[0]["id"] == "root" and components[0]["component"] == "Column"
    # What the compiler emits passes the same check a hand-written screen does.
    assert a2ui.check(messages) == []


def test_an_express_block_that_does_not_compile_says_why():
    pytest.importorskip("a2ui.core.catalog")
    with pytest.raises(a2ui.ScreenSyntaxError) as caught:
        a2ui.compile_express('<a2ui>\nroot = NoSuchComponent(Text("hi"))\n</a2ui>', "pick-1")
    assert "NoSuchComponent" in str(caught.value)


def test_an_express_block_without_a_root_says_so():
    pytest.importorskip("a2ui.core.catalog")
    with pytest.raises(a2ui.ScreenSyntaxError):
        a2ui.compile_express("<a2ui>\n</a2ui>", "pick-1")


def test_an_express_screen_reaches_the_phone_as_messages(tmp_path):
    pytest.importorskip("a2ui.core.catalog")
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_show_ui"])
    seen = {}

    def answer():
        deadline = time.time() + 5
        while time.time() < deadline:
            waiting = phones.pending(PHONE)
            if waiting:
                seen["args"] = waiting[0]["args"]
                phones.answer(PHONE, waiting[0]["id"], {"shown": True}, None)
                return
            time.sleep(0.02)

    threading.Thread(target=answer, daemon=True).start()
    out = json.loads(phones.call("phone_show_ui", {
        "express": '<a2ui>\nroot = Text("hi")\n</a2ui>', "surface": "pick-9", "wait": False,
    }))
    assert out == {"shown": True}
    assert seen["args"]["messages"][0]["createSurface"]["surfaceId"] == "pick-9"
    # The block itself is not what the phone draws.
    assert "express" not in seen["args"]


def test_a_call_with_neither_messages_nor_express_is_refused(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_show_ui"])
    out = json.loads(phones.call("phone_show_ui", {"wait": False}))
    assert "express" in out["error"] and "messages" in out["error"]
    assert phones.pending(PHONE) == []


def test_express_without_a_surface_is_refused(tmp_path):
    phones = Phones(Store(tmp_path))
    phones.report(PHONE, KEY, "Pixel", ["phone_show_ui"])
    out = json.loads(phones.call("phone_show_ui", {
        "express": '<a2ui>\nroot = Text("hi")\n</a2ui>', "wait": False,
    }))
    assert "surface" in out["error"]
    assert phones.pending(PHONE) == []


def test_the_express_page_is_the_generated_skill():
    page = (a2ui._DATA / "express_skill.md").read_text(encoding="utf-8")
    assert page.startswith("# A2UI Express DSL Output Contract")
    assert "## Positional Component Signatures" in page
    from herald_push import screen_catalog_build

    pytest.importorskip("a2ui.core.catalog")
    assert page == screen_catalog_build.express_skill()
