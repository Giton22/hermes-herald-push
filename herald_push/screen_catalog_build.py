"""Builds the screen documentation the agent reads from the catalog Kyllene draws with.

The component list in the tool's description and the reference beside it are generated, not written by hand: when
the catalog changes, these change with it, and ``tests/test_a2ui.py`` fails until the generated file is rebuilt.

Run ``python -m herald_push.screen_catalog_build`` (or ``scripts/build-screen-catalog.py``) from the plugin root.
"""

import json
from typing import Any, Dict, List

from . import a2ui

# Properties every component carries; naming them in each entry would bury what makes the component itself.
_COMMON = frozenset({"id", "component", "weight", "accessibility"})

# An enum whose values read longer than this is a layout or icon list, not a choice the model picks from a
# signature: the compact line names the property and the reference carries the values.
_COMPACT_ENUM_CHARS = 40


def _values(values: List[str], compact: bool) -> str:
    joined = ", ".join(values)
    return "" if compact and len(joined) > _COMPACT_ENUM_CHARS else joined


def _definitions() -> Dict[str, Any]:
    """The catalog's ``$defs`` plus the common types, so a ``$ref`` can be followed to its schema."""
    merged = dict(a2ui.common_types()["$defs"])
    merged.update(a2ui.catalog().get("$defs", {}))
    return merged


def _resolve(schema: Any) -> Any:
    """One level of ``$ref``, enough for the catalog's shapes (a branch may still be an anyOf)."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 8:
        ref = schema["$ref"]
        pointer = ref.split("#", 1)[1] if "#" in ref else ""
        node: Any = _definitions()
        for part in filter(None, pointer.split("/")):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
        seen += 1
    return schema


def _shape(schema: Any, compact: bool = False) -> str:
    """How a property's value reads in a signature: its enum values, or the kind of thing it takes."""
    node = _resolve(schema)
    if not isinstance(node, dict):
        return ""
    for branch in node.get("anyOf") or node.get("oneOf") or []:
        resolved = _resolve(branch)
        if isinstance(resolved, dict) and resolved.get("enum"):
            return _values(list(map(str, resolved["enum"])), compact)
    if node.get("enum"):
        return _values(list(map(str, node["enum"])), compact)
    if node.get("type") == "array":
        items = _resolve(node.get("items") or {})
        if isinstance(items, dict) and items.get("properties"):
            return f"[{{{', '.join(list(items['properties'])[:3])}}}]"
        return "" if compact else "[...]"
    kind = node.get("type")
    return "" if compact or kind in (None, "object") else str(kind)


def _properties(name: str) -> List[tuple]:
    """``(property, schema, description)`` for one component, in catalog order, common ones left out."""
    document = a2ui.catalog()
    entry = document["components"][name]
    out: List[tuple] = []
    seen = set()
    for branch in entry.get("allOf", [entry]):
        node = _resolve(branch)
        for prop, schema in (node.get("properties") or {}).items():
            if prop in _COMMON or prop in seen:
                continue
            seen.add(prop)
            description = schema.get("description") if isinstance(schema, dict) else ""
            out.append((prop, schema, description or ""))
    return out


def component_line(name: str, compact: bool = False) -> str:
    """``Button {child, variant: default, primary, borderless, action}`` — one component, one clause."""
    parts = []
    for prop, schema, _ in _properties(name):
        shape = _shape(schema, compact)
        parts.append(f"{prop}: {shape}" if shape else prop)
    return f"{name} {{{', '.join(parts)}}}" if parts else name


def components_line() -> str:
    """The whole catalog on one line, for the tool description the model reads on every call."""
    return ", ".join(component_line(name, compact=True) for name in a2ui.component_names())


def reference() -> str:
    """A fuller page: every component, its properties and what the catalog says they mean."""
    lines = [
        "# A2UI v0.9 screens — the Basic Catalog Kyllene draws",
        "",
        f"Catalog: `{a2ui.CATALOG_ID}`. {len(a2ui.component_names())} components.",
        "Messages, in order: `createSurface`, `updateComponents` (a flat list; one component's id is `root`),",
        "`updateDataModel`, `deleteSurface`. A property bound to the data model is `{\"path\": \"/name\"}`.",
        "",
    ]
    for name in a2ui.component_names():
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"`{component_line(name)}`")
        lines.append("")
        for prop, schema, description in _properties(name):
            shape = _shape(schema)
            detail = f" — {description}" if description else ""
            lines.append(f"- **{prop}**" + (f" ({shape})" if shape else "") + detail)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def express_skill() -> str:
    """The SDK's generated skill for this catalog: the Express grammar, then every component and function signature.

    Generated at build time, not run time: the plugin compiles Express with the SDK, but what the model reads is a
    file, so a gateway without the SDK still serves the same screens.
    """
    from a2ui.inference_formats.express import ExpressFormatFactory
    from a2ui.skill import SkillGenerator

    skill = SkillGenerator(ExpressFormatFactory().create_format([a2ui._catalog_object()])).generate_skill(name="a2ui")
    return skill.content


def main() -> None:
    """Writes the two artifacts: the line the tool description carries, and the page the agent can read."""
    module = a2ui._DATA.parent / "screen_catalog.py"
    module.write_text(
        '"""Generated by ``herald_push/screen_catalog_build.py`` from ``a2ui_data/catalog.json`` — do not edit.\n'
        "\n"
        "The catalog is the app's; this string is what the agent is told about it. Rebuild after re-vendoring the\n"
        'catalog: ``python -m herald_push.screen_catalog_build``.\n"""\n\n'
        f"COMPONENTS = {json.dumps(components_line(), ensure_ascii=False)}\n",
        encoding="utf-8",
    )
    page = a2ui._DATA / "screen_reference.md"
    page.write_text(reference(), encoding="utf-8")
    skill = a2ui._DATA / "express_skill.md"
    skill.write_text(express_skill(), encoding="utf-8")
    print(f"wrote {module}, {page} and {skill}")


if __name__ == "__main__":
    main()
