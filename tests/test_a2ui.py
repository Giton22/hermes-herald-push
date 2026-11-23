"""A screen is checked against the Basic Catalog before it leaves the gateway.

The app renders A2UI v0.9 with ``androidx.a2ui`` and the Basic Catalog, and used to get whatever the model wrote.
These hold the check to the catalog: a screen the phone can draw passes, one it can't says why in words the agent
can act on, and nothing reaches a phone in between.
"""

import json

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
