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
  const all = [];
  let seen = 0;
  for (const el of document.querySelectorAll(SEL)) {
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
  kept.forEach((c, i) => c.el.setAttribute('data-jevcid', 'c' + (i + 1)));
  return {
    total,
    dropped: Math.max(0, total - kept.length),
    candidates: kept.map((c, i) => ({
      cid: 'c' + (i + 1), role: c.role, name: c.name, tag: c.tag, type: c.type,
      href: c.href, placeholder: c.placeholder, scope: c.scope, value: c.value,
      disabled: c.disabled, in_viewport: c.in_viewport,
    })),
  };
}
"""


DEFAULT_MAX_CANDIDATES = 120


def extract(page: Any, limit: int | None = None) -> tuple[list[dict[str, Any]], int, int]:
    """Return (candidates, total_on_page, dropped) for the current page state.

    The list is a shortlist, not the page: in-viewport and form controls first,
    capped at ``limit`` (default 120, the API's hard cap is 254 because ``none``
    takes one of the 255 choices). ``page --max-candidates`` tunes the trade
    between decision speed/cost and coverage.
    """
    raw = page.evaluate(CANDIDATE_JS, {"limit": limit or DEFAULT_MAX_CANDIDATES})
    candidates = [
        make_candidate(
            c["cid"],
            c["role"],
            c["name"],
            tag=c["tag"],
            type=c["type"],
            href=c["href"],
            placeholder=c["placeholder"],
            scope=c["scope"],
            value=c["value"],
            disabled=c["disabled"],
            in_viewport=c["in_viewport"],
        )
        for c in raw["candidates"]
    ]
    return candidates, raw["total"], raw["dropped"]


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


def resolve(page: Any, cid: str, *, timeout_ms: int = 10_000) -> Any:
    """The element extraction stamped for this decision — exact, unambiguous."""
    locator = page.locator(f'[data-jevcid="{cid}"]').first
    locator.wait_for(state="attached", timeout=timeout_ms)
    return locator


def execute(page: Any, cid: str, action: dict[str, Any], *, settle_ms: int = 300) -> None:
    """Perform the recorded action on the chosen element, then let the DOM settle."""
    element = resolve(page, cid)
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


def execute_fp(page: Any, fp: str, action: dict[str, Any], *, settle_ms: int = 300) -> None:
    """Execute an action against the element with this fingerprint, not this position.

    Replay must never act on a shifted candidate: if the fingerprint is gone or
    duplicated, this raises instead of clicking the wrong thing.
    """
    candidates, _, _ = extract(page)
    matches = [c for c in candidates if c["fp"] == fp]
    if len(matches) != 1:
        raise RuntimeError(f"cannot execute: {len(matches)} candidates match {fp!r}")
    execute(page, matches[0]["cid"], action, settle_ms=settle_ms)
