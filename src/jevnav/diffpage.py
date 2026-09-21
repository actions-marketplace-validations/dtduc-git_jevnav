"""Compare two pages: the structure and the computed styles, without a model.

Built for the "here is the new UX, update the codebase" loop: open the mockup
and the running app, snapshot both, and print only what differs — missing
sections, moved or resized elements, changed labels, changed computed styles.
Deterministic and offline from the model's point of view; the caller decides
what to do about each line.
"""

from __future__ import annotations

from typing import Any

from .page import DEFAULT_STYLE_PROPS, outline, styles

BOX_TOLERANCE_PX = 4


def structure_key(element: dict[str, Any]) -> str:
    """What makes an element 'the same element' across two pages.

    The element's *own* text is used when it has one, so a container is not
    reported as changed just because a child disappeared.
    """
    label = element.get("ownText") or ""
    if not label and element.get("leaf", True):
        label = element.get("text") or ""
    label = label or element.get("name") or element.get("id") or ""
    return f"{element['tag']}|{' '.join(str(label).split()).casefold()}"


def structure_diff(
    a: list[dict[str, Any]], b: list[dict[str, Any]], *, tolerance: int = BOX_TOLERANCE_PX
) -> list[dict[str, Any]]:
    """Missing / new / moved elements, comparing by tag + label (multiset)."""
    remaining = list(b)
    differences: list[dict[str, Any]] = []
    for element in a:
        key = structure_key(element)
        match = next((item for item in remaining if structure_key(item) == key), None)
        if match is None:
            differences.append(
                {
                    "kind": "missing",
                    "element": _describe(element),
                    "detail": "not on the other page",
                }
            )
            continue
        remaining.remove(match)
        dx = match["box"][0] - element["box"][0]
        dy = match["box"][1] - element["box"][1]
        dw = match["box"][2] - element["box"][2]
        dh = match["box"][3] - element["box"][3]
        if max(abs(dx), abs(dy), abs(dw), abs(dh)) > tolerance:
            differences.append(
                {
                    "kind": "moved",
                    "element": _describe(element),
                    "detail": f"x{dx:+d} y{dy:+d} w{dw:+d} h{dh:+d}px",
                }
            )
    for element in remaining:
        differences.append(
            {"kind": "new", "element": _describe(element), "detail": "only on the other page"}
        )
    return differences


def style_diff(
    selector: str, a: dict[str, Any], b: dict[str, Any], *, tolerance: int = 0
) -> list[dict[str, Any]]:
    """Property-by-property differences for the elements the selector matches."""
    differences: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(zip(a["elements"], b["elements"], strict=False)):
        label = left.get("text") or right.get("text") or f"{left['element']} #{index}"
        for prop, left_value in left["styles"].items():
            right_value = right["styles"].get(prop)
            if left_value != right_value and not _within_tolerance(
                left_value, right_value, tolerance
            ):
                differences.append(
                    {
                        "kind": "style",
                        "selector": selector,
                        "element": f"{left['element']} [{label}]",
                        "property": prop,
                        "mockup": left_value,
                        "app": right_value,
                    }
                )
    extra = len(a["elements"]) - len(b["elements"])
    if extra:
        differences.append(
            {
                "kind": "style",
                "selector": selector,
                "element": f"{abs(extra)} element(s)",
                "property": "count",
                "mockup": len(a["elements"]),
                "app": len(b["elements"]),
            }
        )
    return differences


def _within_tolerance(left: str, right: str, tolerance: int) -> bool:
    if not tolerance or not left.endswith("px") or not right.endswith("px"):
        return False
    try:
        return abs(float(left[:-2]) - float(right[:-2])) <= tolerance
    except ValueError:
        return False


def compare(
    url_a: str,
    url_b: str,
    *,
    page: Any,
    selector: str = "body",
    style_selector: str = "h1,h2,h3,button,a,input,main,header,footer",
    style_props: list[str] | None = None,
    limit: int = 200,
    style_limit: int = 10,
    tolerance: int = BOX_TOLERANCE_PX,
) -> dict[str, Any]:
    """Snapshot both URLs and return the structure and style differences."""
    page.goto(url_a, wait_until="domcontentloaded")
    page.wait_for_timeout(300 if not url_a.startswith("file:") else 0)
    a_outline = outline(page, selector, limit)
    a_styles = styles(page, style_selector, style_props or DEFAULT_STYLE_PROPS, style_limit)
    title_a = page.title()
    page.goto(url_b, wait_until="domcontentloaded")
    page.wait_for_timeout(300 if not url_b.startswith("file:") else 0)
    b_outline = outline(page, selector, limit)
    b_styles = styles(page, style_selector, style_props or DEFAULT_STYLE_PROPS, style_limit)
    title_b = page.title()
    structure = structure_diff(a_outline["elements"], b_outline["elements"], tolerance=tolerance)
    style = style_diff(style_selector, a_styles, b_styles)
    return {
        "a": {"url": url_a, "title": title_a, "elements": a_outline["count"]},
        "b": {"url": url_b, "title": title_b, "elements": b_outline["count"]},
        "selector": selector,
        "style_selector": style_selector,
        "identical": not structure and not style,
        "counts": {"structure": len(structure), "style": len(style)},
        "structure": structure,
        "style": style,
    }


def _describe(element: dict[str, Any]) -> str:
    label = element.get("text") or element.get("name") or element.get("id") or ""
    label = " ".join(str(label).split())[:60]
    where = f"{element['tag']}" + (f"#{element['id']}" if element.get("id") else "")
    return f"{where} {label!r}".strip()


def render(result: dict[str, Any]) -> str:
    """A markdown report: what a coding agent reads to learn what to change."""

    def cell(text: Any) -> str:
        return str(text).replace("|", "\\|")

    left, right = result["a"], result["b"]
    lines = ["# jevnav diff", ""]
    lines.append(f"- mockup: `{left['url']}` — {left['title']!r}, {left['elements']} elements")
    lines.append(f"- app:    `{right['url']}` — {right['title']!r}, {right['elements']} elements")
    if result["identical"]:
        lines.append("")
        lines.append(f"- **identical** in `{result['selector']}` and `{result['style_selector']}`")
        return "\n".join(lines) + "\n"
    lines.append(
        f"- differences: **{result['counts']['structure']}** structure, "
        f"**{result['counts']['style']}** style"
    )
    if result["structure"]:
        lines += [
            "",
            f"## Structure (`{result['selector']}`)",
            "",
            "| kind | element | detail |",
            "|---|---|---|",
        ]
        for item in result["structure"]:
            lines.append(f"| {item['kind']} | {cell(item['element'])} | {cell(item['detail'])} |")
    if result["style"]:
        lines += [
            "",
            f"## Styles (`{result['style_selector']}`)",
            "",
            "| element | property | mockup | app |",
            "|---|---|---|---|",
        ]
        for item in result["style"]:
            lines.append(
                f"| {cell(item['element'])} | {item['property']} | "
                f"{cell(item['mockup'])} | {cell(item['app'])} |"
            )
    return "\n".join(lines) + "\n"


__all__ = ["compare", "render", "structure_diff", "style_diff", "structure_key"]
