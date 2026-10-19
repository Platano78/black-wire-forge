"""The prototype's legacy-only enum check, kept as a CHARACTERIZATION of the blind spot the
spike found (2026-10-16): it reads only the old dropdown shape [[option, ...], {...}], so an
input in ComfyUI's newer V3 shape ["COMBO", {"options": [...]}] is taken for a typed value and
never enum-checked -- a model file the lane does not list passes. tests/test_autopack_validate.py
asserts this blind spot next to the fixed validator. Do not use this in the app."""


def legacy_enum_problems(graph, object_info):
    """Node ids and inputs whose literal value is outside a LEGACY-shaped dropdown."""
    problems = []
    for nid, node in graph.items():
        schema = (object_info.get(node.get("class_type")) or {}).get("input", {})
        specs = {**(schema.get("required") or {}), **(schema.get("optional") or {})}
        for name, value in node.get("inputs", {}).items():
            spec = specs.get(name)
            if isinstance(value, list) or not isinstance(spec, list) or not spec:
                continue
            if node.get("class_type") == "LoadImage" and name == "image":
                continue
            if isinstance(spec[0], list) and value not in spec[0]:
                problems.append((nid, name, value))
    return problems
