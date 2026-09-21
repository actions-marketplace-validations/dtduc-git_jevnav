"""The page side: candidate extraction, locators and action execution.

The model only ever sees the candidate list produced here, so extraction is
the accuracy ceiling of the whole tool. The list is capped at 254 (the choice
limit is 255 and "none" takes one slot), in-viewport elements first. Names are
computed with the accessible name rules that matter in practice (aria-label, aria-labelledby, native
labels, value/placeholder fallbacks); every candidate carries a scope (nearest
legend/heading) so identical labels stay distinguishable.
"""

from __future__ import annotations

from typing import Any

from .trace import make_candidate

PLAYWRIGHT_ROLES = {
    "button",
    "link",
    "tab",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "checkbox",
    "radio",
    "switch",
    "textbox",
    "searchbox",
    "combobox",
    "listbox",
    "option",
    "slider",
    "spinbutton",
    "img",
    "heading",
}

CANDIDATE_JS = r"""
(args) => {
  const LIMIT = args.limit;
  const PREFIX = args.prefix || 'f0';
  const SEL = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea',
    '[contenteditable="true"]',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="menuitemcheckbox"]', '[role="menuitemradio"]', '[role="checkbox"]',
    '[role="radio"]', '[role="switch"]', '[role="searchbox"]', '[role="textbox"]',
    '[role="combobox"]', '[role="listbox"]', '[role="option"]', '[role="slider"]'
  ].join(',');

  const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.split(/\s+/)[0];
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    return 'generic';
  };

  const accName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map((id) => { const node = document.getElementById(id); return node ? textOf(node) : ''; })
        .filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.labels && el.labels.length) {
      for (const label of el.labels) { const t = textOf(label); if (t) return t; }
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(type)) return (el.value || '').trim();
      return (el.getAttribute('placeholder') || el.getAttribute('title') ||
              el.getAttribute('name') || el.getAttribute('id') || '').trim();
    }
    if (tag === 'select' || tag === 'textarea') {
      return (el.getAttribute('placeholder') || el.getAttribute('title') ||
              el.getAttribute('name') || el.getAttribute('id') || '').trim() || textOf(el);
    }
    // an icon-only control still has a name: the icon's alt, an svg title, or title
    const iconAlt = el.querySelector('img[alt]');
    if (iconAlt && iconAlt.getAttribute('alt').trim()) return iconAlt.getAttribute('alt').trim();
    const svgTitle = el.querySelector('svg > title, svg title');
    if (svgTitle && textOf(svgTitle)) return textOf(svgTitle);
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    return textOf(el);
  };

  const fieldValue = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'select') {
      const option = el.selectedOptions && el.selectedOptions[0];
      return option && option.value ? option.textContent.trim().slice(0, 40) : null;
    }
    if (type === 'checkbox' || type === 'radio') {
      return el.checked ? 'checked' : null;
    }
    if (type === 'password') {
      return el.value ? '••••' : null;
    }
    const raw = (el.value || '').trim();
    return raw ? raw.slice(0, 40) : null;
  };

  const scopeOf = (el) => {
    const cut = (t) => (t || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector('legend');
      const t = legend ? cut(textOf(legend)) : '';
      if (t) return t;
    }
    const form = el.closest('form');
    if (form) {
      const t = cut(form.getAttribute('aria-label') || form.getAttribute('name') ||
                    form.getAttribute('id'));
      if (t) return t;
    }
    const region = el.closest('nav,header,footer,aside,section,main,dialog');
    if (region && region !== document.body) {
      const heading = region.querySelector('h1,h2,h3,h4,legend');
      const t = cut((heading ? textOf(heading) : '') || region.getAttribute('aria-label') || '');
      if (t) return t;
    }
    return null;
  };

  // Order the shortlist by how likely a human would act on it: things in the
  // viewport first, form controls before buttons before links, DOM order last.
  // Measured on Hacker News (199 candidates -> 40): same accuracy, 2.8x faster
  // on a cold decision and 3.8x fewer input tokens.
  const ROLE_RANK = { textbox: 0, searchbox: 0, combobox: 0, spinbutton: 0, checkbox: 0, radio: 0,
                      switch: 0, button: 1, tab: 1, menuitem: 1, link: 2 };
  // walk the light DOM and every open shadow root: design systems put controls there
  const collect = (rootNode, out) => {
    for (const el of rootNode.querySelectorAll(SEL)) out.push(el);
    for (const el of rootNode.querySelectorAll('*')) {
      if (el.shadowRoot) collect(el.shadowRoot, out);
    }
    return out;
  };
  const all = [];
  let seen = 0;
  for (const el of collect(document, [])) {
    if (++seen > 2000) break;
    if (el.closest('[aria-hidden="true"]')) continue;
    const tag = el.tagName.toLowerCase();
    const inputType = (el.getAttribute('type') || '').toLowerCase();
    const isFile = tag === 'input' && inputType === 'file';
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    // a file input is usually hidden behind a styled label; Playwright can still set it
    if (!isFile) {
      if (!rect.width || !rect.height) continue;
      if (style.visibility === 'hidden' || style.display === 'none') continue;
    }
    const role = roleOf(el);
    if (role === 'generic') continue;
    const name = accName(el);
    if (!name) continue;
    const href = el.getAttribute('href');
    all.push({
      role, name: name.slice(0, 120),
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      href: href ? href.slice(0, 120) : null,
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 60) || null,
      scope: scopeOf(el),
      value: fieldValue(el),
      disabled: !!el.disabled,
      in_viewport: rect.bottom > 0 && rect.top < innerHeight &&
                   rect.right > 0 && rect.left < innerWidth,
      el,
    });
  }
  const total = all.length;
  all.forEach((c, i) => { c.dom_index = i; });
  all.sort((a, b) =>
    Number(b.in_viewport) - Number(a.in_viewport) ||
    (ROLE_RANK[a.role] ?? 3) - (ROLE_RANK[b.role] ?? 3) ||
    a.dom_index - b.dom_index);
  const kept = all.slice(0, Math.min(LIMIT, 254));
  // clear every stamp first: an element that fell out of the shortlist used to
  // keep its old cid, so a later .first() could match it and act on the wrong node
  for (const el of document.querySelectorAll('[data-jevcid]')) el.removeAttribute('data-jevcid');
  kept.forEach((c, i) => c.el.setAttribute('data-jevcid', PREFIX + ':c' + (i + 1)));
  return {
    total,
    dropped: Math.max(0, total - kept.length),
    candidates: kept.map((c, i) => ({
      cid: PREFIX + ':c' + (i + 1), role: c.role, name: c.name, tag: c.tag, type: c.type,
      href: c.href, placeholder: c.placeholder, scope: c.scope, value: c.value,
      disabled: c.disabled, in_viewport: c.in_viewport,
    })),
  };
}
"""


DEFAULT_MAX_CANDIDATES = 120


def candidate_key(candidate: dict[str, Any]) -> str:
    """The key to look a candidate up by: fingerprint, then which frame it lives in."""
    return f"{candidate.get('frame', 0)}|{candidate['fp']}"


def extract(page: Any, limit: int | None = None) -> tuple[list[dict[str, Any]], int, int]:
    """Return (candidates, total_on_page, dropped) for the current page state.

    Every frame is read, main frame first, and open shadow roots are pierced
    (payment widgets and design systems live there). Cids are namespaced per
    frame (``f0:c3``, ``f1:c7``) so a cid means one element, on one frame.

    The list is a shortlist, not the page: in-viewport and form controls first,
    capped at ``limit`` (default 120, the API's hard cap is 254 because ``none``
    takes one of the 255 choices). ``page --max-candidates`` tunes the trade
    between decision speed/cost and coverage.
    """
    per_frame_limit = limit or DEFAULT_MAX_CANDIDATES
    candidates: list[dict[str, Any]] = []
    total = 0
    dropped = 0
    for frame_index, frame in enumerate(page.frames):
        try:
            raw = frame.evaluate(
                CANDIDATE_JS, {"limit": per_frame_limit, "prefix": f"f{frame_index}"}
            )
        except Exception:  # detached, sandboxed or otherwise unreachable frame
            continue
        total += raw["total"]
        dropped += raw["dropped"]
        for item in raw["candidates"]:
            candidates.append(
                make_candidate(
                    item["cid"],
                    item["role"],
                    item["name"],
                    tag=item["tag"],
                    type=item["type"],
                    href=item["href"],
                    placeholder=item["placeholder"],
                    scope=item["scope"],
                    value=item["value"],
                    disabled=item["disabled"],
                    in_viewport=item["in_viewport"],
                    frame=frame_index,
                )
            )
    return candidates, total, dropped


def by_cid(candidates: list[dict[str, Any]], cid: str) -> dict[str, Any] | None:
    return next((c for c in candidates if c["cid"] == cid), None)


def locator_for(page: Any, candidate: dict[str, Any]) -> tuple[str, bool]:
    """Standard Playwright locator for a candidate, and whether it matches exactly one element.

    This is the locator a human would write from the candidate list, and what
    ``jevnav mcp`` hands back to an agent. ``False`` means the accessible-name
    computation here diverged from Playwright's on this element.
    """
    role, name = candidate["role"], candidate["name"]
    if role in PLAYWRIGHT_ROLES:
        selector = f'role={role}[name="{name}"]'
        try:
            count = page.get_by_role(role, name=name, exact=True).count()
        except Exception:
            count = -1
        return selector, count == 1
    selector = f'text="{name}"'
    try:
        count = page.get_by_text(name, exact=True).count()
    except Exception:
        count = -1
    return selector, count == 1


def frame_of(page: Any, frame_index: int) -> Any:
    """The Playwright frame behind an index; the main frame when it is gone."""
    try:
        return page.frames[frame_index]
    except IndexError:
        return page.main_frame


def locator_by_fp(page: Any, fp: str, *, frame_index: int = 0, timeout_ms: int = 10_000) -> Any:
    """The single element with this fingerprint in this frame, or a loud failure.

    Identity is the fingerprint everywhere else (decisions, traces, replay), so
    acting must use it too: a position-based lookup can silently point at an
    element that only *used to* be the chosen one.
    """
    candidates, _, _ = extract(page)
    matches = [c for c in candidates if c["fp"] == fp and c.get("frame", 0) == frame_index]
    if len(matches) != 1:
        raise RuntimeError(
            f"cannot act: {len(matches)} candidates match {fp!r} in frame {frame_index} "
            "(expected exactly one)"
        )
    frame = frame_of(page, frame_index)
    locator = frame.locator(f'[data-jevcid="{matches[0]["cid"]}"]')
    if locator.count() != 1:
        raise RuntimeError(f"cannot act: the stamp for {fp!r} is not unique in its frame")
    locator.wait_for(state="attached", timeout=timeout_ms)
    return locator


def resolve(page: Any, cid: str, *, timeout_ms: int = 10_000) -> Any:
    """A stamped element by cid — refuses when the stamp is not unique."""
    locator = page.locator(f'[data-jevcid="{cid}"]')
    if locator.count() != 1:
        raise RuntimeError(f"cannot resolve {cid!r}: {locator.count()} elements carry that stamp")
    locator = locator.first
    locator.wait_for(state="attached", timeout=timeout_ms)
    return locator


def execute(
    page: Any, candidate: dict[str, Any], action: dict[str, Any], *, settle_ms: int = 300
) -> None:
    """Perform an action on a candidate, resolved by fingerprint, then let the DOM settle."""
    execute_fp(
        page, candidate["fp"], action, frame_index=candidate.get("frame", 0), settle_ms=settle_ms
    )


def execute_fp(
    page: Any, fp: str, action: dict[str, Any], *, frame_index: int = 0, settle_ms: int = 300
) -> None:
    """Execute an action against the element with this fingerprint, not this position."""
    element = locator_by_fp(page, fp, frame_index=frame_index)
    kind = action.get("type", "click")
    if kind == "click":
        element.click()
    elif kind == "fill":
        element.fill(action["value"])
    elif kind == "select":
        element.select_option(action["value"])
    elif kind == "check":
        element.check()
    elif kind == "hover":
        element.hover()
    elif kind == "press":
        element.press(action["key"])
    elif kind == "none":
        pass
    else:
        raise ValueError(f"unknown action type {kind!r}")
    if settle_ms:
        page.wait_for_timeout(settle_ms)


OUTLINE_JS = r"""(args) => {
  const root = document.querySelector(args.selector) || document.body;
  const SEL = 'h1,h2,h3,h4,h5,h6,main,header,footer,nav,section,article,'
    + 'form,label,button,a,p,li,img,input,select,textarea';
  const out = [];
  for (const el of root.querySelectorAll(SEL)) {
    if (out.length >= args.limit) break;
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const raw = el.innerText || el.textContent || '';
    const text = raw.replace(/\s+/g, ' ').trim().slice(0, 80);
    // text of this element only, so a container is not "changed" when a child is
    const ownText = [...el.childNodes]
      .filter((node) => node.nodeType === 3)
      .map((node) => node.textContent)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 80);
    const name = el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                 el.getAttribute('alt') || el.getAttribute('name') || null;
    out.push({
      tag,
      level: /^h[1-6]$/.test(tag) ? Number(tag[1]) : null,
      text: text || null,
      ownText: ownText || null,
      leaf: !el.querySelector(SEL),
      name,
      id: el.id || null,
      classes: (el.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 4),
      box: [Math.round(rect.x), Math.round(rect.y),
            Math.round(rect.width), Math.round(rect.height)],
    });
  }
  return {selector: args.selector, count: out.length, elements: out};
}"""

STYLES_JS = r"""(args) => {
  const nodes = [...document.querySelectorAll(args.selector)].slice(0, args.limit);
  const props = args.props;
  return nodes.map((el) => {
    const style = getComputedStyle(el);
    const styles = {};
    for (const prop of props) {
      let value = style.getPropertyValue(prop).trim();
      // round every px number so fractional layout noise is not read as a change
      value = value.replace(/(-?\d+\.\d+)px/g, (_m, number) => Math.round(Number(number)) + 'px');
      styles[prop] = value;
    }
    const rect = el.getBoundingClientRect();
    return {
      element: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''),
      text: (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60) || null,
      box: [Math.round(rect.width), Math.round(rect.height)],
      styles,
    };
  });
}"""

DEFAULT_STYLE_PROPS = [
    "display",
    "position",
    "width",
    "height",
    "color",
    "background-color",
    "font-size",
    "font-weight",
    "font-family",
    "line-height",
    "padding",
    "margin",
    "border-radius",
    "gap",
    "flex-direction",
    "align-items",
    "justify-content",
    "grid-template-columns",
]


def outline(page: Any, selector: str = "body", limit: int = 200) -> dict[str, Any]:
    """Structural summary of a region: tags, headings, text, accessible names, boxes."""
    return page.evaluate(OUTLINE_JS, {"selector": selector, "limit": limit})


def styles(
    page: Any, selector: str, props: list[str] | None = None, limit: int = 10
) -> dict[str, Any]:
    """Computed styles of the matching elements, as the browser resolved them."""
    return {
        "selector": selector,
        "elements": page.evaluate(
            STYLES_JS, {"selector": selector, "props": props or DEFAULT_STYLE_PROPS, "limit": limit}
        ),
    }
