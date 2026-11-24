"""A2UI screens: the Basic Catalog Kyllene draws, and the check a screen gets before it leaves the gateway.

The app renders A2UI v0.9 with ``androidx.a2ui`` against the Basic Catalog, and the tool used to hand it whatever
the model wrote: nothing on this side looked at a screen, so a misspelled component or a missing property reached
the phone and came back as an error after a wait. The two documents the app is built from are vendored beside this
module, so the check is offline, deterministic and matches the catalog the renderer knows.

What the app already rejects on its own (a cycle, nesting past 24, more than 500 components, a message that isn't
an object) is not repeated here. What it cannot know without the catalog is: an unknown component, a missing or
mistyped property, a ``root`` that isn't there or leads nowhere, a child id that was never defined, and a
``catalogId`` that isn't this one.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"
COMMON_TYPES_ID = "https://a2ui.org/specification/v0_9/common_types.json"
# ``common_types.json`` reaches the catalog's ``anyFunction`` union as ``catalog.json#/$defs/anyFunction``, a
# sibling that the published specification does not serve (404). Registering the same document under that URL is
# what lets a component carrying a function call or a check rule validate, without editing the vendored files.
CATALOG_SIBLING_ID = "https://a2ui.org/specification/v0_9/catalog.json"

# At most this many messages a call: a screen, not a stream.
MAX_MESSAGES = 100
# The agent reads these, so a screen wrong in ten places still gets a short answer.
MAX_PROBLEMS = 4

_DATA = Path(__file__).with_name("a2ui_data")

# One body per message; anything else is not a message the app knows.
_KINDS = ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")

# Keys whose values name other components: children, a card's or button's child, a modal's parts.
_CHILD_KEYS = frozenset({"child", "children", "trigger", "content", "componentId"})


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads((_DATA / "catalog.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def common_types() -> dict:
    return json.loads((_DATA / "common_types.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _registry() -> Registry:
    document = catalog()
    documents = {
        CATALOG_ID: document,
        CATALOG_SIBLING_ID: document,
        COMMON_TYPES_ID: common_types(),
    }
    return Registry().with_resources(
        [(uri, Resource.from_contents(doc, default_specification=DRAFT202012)) for uri, doc in documents.items()]
    )


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    """The schema one component is checked against.

    The catalog's own entry, carrying the catalog's ``$defs`` and id so its relative refs resolve inside it. Its
    ``unevaluatedProperties`` is dropped: over these cross-document ``allOf`` refs jsonschema collects no
    annotations, so it reports every valid property as unexpected. Unknown properties are checked instead by
    :func:`_allowed`, which reads the same schemas.
    """
    document = catalog()
    schema = {**document["components"][name], "$id": CATALOG_ID, "$defs": document.get("$defs", {})}
    schema.pop("unevaluatedProperties", None)
    return Draft202012Validator(schema, registry=_registry())


@lru_cache(maxsize=None)
def _allowed(name: str) -> frozenset:
    """Every property this component's schemas define, its ``allOf`` refs followed into the catalog and common types."""
    document, common = catalog(), common_types()
    allowed = set()
    for branch in document["components"][name].get("allOf", [document["components"][name]]):
        ref = branch.get("$ref")
        if not ref:
            allowed.update(branch.get("properties", {}))
            continue
        uri, _, pointer = ref.partition("#")
        node = common if uri == COMMON_TYPES_ID else document
        for part in filter(None, pointer.split("/")):
            node = node[part]
        allowed.update(node.get("properties", {}))
    return frozenset(allowed)


def component_names() -> List[str]:
    return list(catalog()["components"])


class ScreenSyntaxError(ValueError):
    """An A2UI Express block that doesn't compile, in words the agent can fix."""


@lru_cache(maxsize=1)
def _catalog_object():
    """The SDK's model of the vendored catalog. Imported here so the plugin still loads without the SDK."""
    from a2ui.core.catalog import Catalog

    return Catalog.from_json(catalog())


def compile_express(text: str, surface_id: str) -> List[dict]:
    """The wire messages an A2UI Express block compiles to, validated against the catalog as it compiles.

    The app never sees Express: this runs on the gateway, so a screen written in the compact DSL arrives as the
    same ``createSurface`` / ``updateComponents`` JSON as one written out by hand — for about half the tokens.
    """
    try:
        from a2ui.inference_formats.express import ExpressFormatFactory
        from a2ui.processor.processor import A2uiRequestProcessor
    except ImportError as error:
        raise ScreenSyntaxError(
            "A2UI Express is not available on this gateway (the a2ui-agent-sdk package is missing); "
            "send messages instead."
        ) from error

    try:
        processor = A2uiRequestProcessor(
            [_catalog_object()], format_factory=ExpressFormatFactory(surface_id=surface_id)
        )
        parts = processor.parse_response(text)
    except Exception as error:  # the SDK's own error types all subclass A2uiError, but a bad block can raise anything
        raise ScreenSyntaxError(_first_line(error)) from error
    messages = [
        message.model_dump(by_alias=True, exclude_none=True)
        for part in parts
        for message in (getattr(part, "a2ui", None) or [])
    ]
    if not messages:
        raise ScreenSyntaxError("the block compiled to no messages; assign the screen to 'root' inside <a2ui>…</a2ui>.")
    return messages


def _first_line(error: Exception) -> str:
    """The SDK explains a rejected block over several lines; the agent needs the first one."""
    text = " ".join(str(error).split())
    return text[:300] if text else type(error).__name__


def _kind_of(message: Any) -> Optional[str]:
    if not isinstance(message, dict):
        return None
    found = [kind for kind in _KINDS if kind in message]
    return found[0] if len(found) == 1 else None


def _describe(error) -> str:
    """One jsonschema error in words the model can act on, without the schema's noise."""
    path = ".".join(str(part) for part in error.absolute_path if part != "components")
    where = f" ({path})" if path else ""
    if error.validator == "required":
        missing = [name for name in error.validator_value if name not in (error.instance or {})]
        return f"missing {', '.join(repr(name) for name in missing)}{where}"
    if error.validator == "enum":
        return f"{path or 'value'} must be one of {', '.join(map(str, error.validator_value))}"
    if error.validator in ("additionalProperties", "unevaluatedProperties"):
        unexpected = ", ".join(re.findall(r"'([^']+)'", error.message))
        return f"has no property {unexpected}{where}"
    return f"{error.message}{where}"


def _child_ids(component: dict) -> List[str]:
    """Every id this component points at, wherever it keeps them (``children``, ``child``, a modal's parts, tabs)."""
    found: List[str] = []

    def walk(key: str, value: Any) -> None:
        if isinstance(value, str):
            if key in _CHILD_KEYS:
                found.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(key, item)
        elif isinstance(value, dict):
            for inner_key, inner in value.items():
                walk(inner_key, inner)

    for key, value in component.items():
        if key not in ("id", "component"):
            walk(key, value)
    return found


def check(messages: Any) -> List[str]:
    """Why Kyllene won't draw these messages, in words for the agent; empty when it will.

    Checks the envelope, then each component against the Basic Catalog, then that a fresh surface has a ``root``
    that leads somewhere. A component the renderer cannot reach from ``root`` is not an error: ``androidx.a2ui``
    draws the surface and ignores it (A2UI's own ``31_incremental-dashboard`` sample does exactly this and draws
    fine), so only a surface that reaches nothing past ``root`` — one that would come up empty — is reported.
    Reachability is skipped for a call that only updates a surface: the rest of that screen is already on the
    phone, where this process cannot see it.
    """
    if not isinstance(messages, list) or not messages:
        return ["messages must be a list of A2UI message objects, in the order they apply."]
    if len(messages) > MAX_MESSAGES:
        return [f"messages has {len(messages)}; send at most {MAX_MESSAGES} for one screen."]

    problems: List[str] = []
    surfaces: Dict[str, Dict[str, dict]] = {}
    declared: Dict[str, set] = {}
    fresh: set = set()
    names_listed = False

    def add(problem: str) -> None:
        # The same fault can surface through several allOf branches; the agent only needs it once.
        if len(problems) < MAX_PROBLEMS and problem not in problems:
            problems.append(problem)

    for index, message in enumerate(messages):
        kind = _kind_of(message)
        if kind is None:
            add(f"messages[{index}] must be an object with exactly one of {', '.join(_KINDS)}.")
            continue
        body = message[kind]
        if not isinstance(body, dict):
            add(f"messages[{index}].{kind} must be an object.")
            continue
        surface_id = body.get("surfaceId")
        if not isinstance(surface_id, str) or not surface_id:
            add(f"messages[{index}].{kind} needs a surfaceId.")
            continue

        if kind == "createSurface":
            catalog_id = body.get("catalogId")
            if catalog_id != CATALOG_ID:
                add(f"createSurface.catalogId must be {CATALOG_ID!r} (got {catalog_id!r}); "
                    "Kyllene draws the Basic Catalog only.")
            fresh.add(surface_id)

        elif kind == "updateComponents":
            components = body.get("components")
            if not isinstance(components, list):
                add(f"messages[{index}].updateComponents.components must be a list.")
                continue
            known = surfaces.setdefault(surface_id, {})
            for component in components:
                if not isinstance(component, dict):
                    add(f"messages[{index}].updateComponents.components has an entry that isn't an object.")
                    continue
                component_id = component.get("id")
                name = component.get("component")
                if not isinstance(component_id, str) or not component_id:
                    add(f"messages[{index}].updateComponents: a component has no id.")
                    continue
                declared.setdefault(surface_id, set()).add(component_id)
                if not isinstance(name, str) or name not in catalog()["components"]:
                    # The full list once: the model needs a valid name to swap in, not the same list four times.
                    names = f" Use one of: {', '.join(component_names())}." if not names_listed else ""
                    names_listed = True
                    add(f"component {component_id!r} is {name!r}, which isn't in the Basic Catalog.{names}")
                    continue
                for error in sorted(_validator(name).iter_errors(component), key=lambda e: list(e.absolute_path)):
                    add(f"component {component_id!r} ({name}) {_describe(error)}")
                unknown = [key for key in component if key not in _allowed(name)]
                if unknown:
                    add(f"component {component_id!r} ({name}) has no property {', '.join(unknown)}; "
                        f"it takes {', '.join(sorted(_allowed(name)))}.")
                known[component_id] = component

        elif kind == "updateDataModel":
            # ``path`` is optional and means the root when it is absent, as the specification's own examples do.
            path = body.get("path", "/")
            if not isinstance(path, str) or not path.startswith("/"):
                add(f"messages[{index}].updateDataModel.path must be a JSON Pointer such as '/' or '/name'.")
            if "value" not in body:
                add(f"messages[{index}].updateDataModel needs a value.")

    for surface_id in fresh:
        known = surfaces.get(surface_id) or {}
        if not known:
            continue
        if "root" not in known:
            add(f"surface {surface_id!r} has no component with id 'root'; every screen starts at 'root'.")
            continue
        reached, queue, dangling = {"root"}, ["root"], []
        named = declared.get(surface_id) or set(known)
        while queue:
            for child in _child_ids(known[queue.pop()]):
                if child not in named:
                    if child not in dangling:
                        dangling.append(child)
                elif child in known and child not in reached:
                    reached.add(child)
                    queue.append(child)
        if dangling:
            add(f"surface {surface_id!r} names components that don't exist: "
                f"{', '.join(dangling[:6])}{' …' if len(dangling) > 6 else ''}. "
                "Define them, or point at ids that are there.")
        if reached == {"root"} and len(known) > 1:
            add(f"surface {surface_id!r} draws nothing: no component is reachable from 'root'. "
                "Name children in 'root'.")

    return problems
