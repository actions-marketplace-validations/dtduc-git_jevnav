# Security

## Reporting

Open a [private security advisory](https://github.com/dtduc-git/jevnav/security/advisories/new)
or email dtduc.contact@gmail.com. Please do not open a public issue for a
vulnerability.

## What leaves your machine

`jevnav run` and `jevnav go` send one request per decision to your configured
Jev endpoint (`TYPESAFE_BASE_URL`, default `https://api.typesafe.ai`). That
request contains the page URL, the page title, the candidate list — element
roles, accessible names, `href`s, placeholders — and, for candidates that hold
one, the current field value (password fields are masked to `••••`).

`jevnav go` also sends a digest of the page's visible text (headings, labels,
paragraph text; at most 700 characters), because judging "is the goal done?"
needs the page, not just its controls. Scripted flows (`jevnav run`) send no
page text. Neither ever sends screenshots, cookies or storage.

`jevnav replay` sends nothing: it re-resolves recorded decisions against the
page locally, with no model call. There is no telemetry, no analytics and no
hosted component.

## Secrets

A flow value written as `${NAME}` is read from the environment at run time and
recorded as `value_from_env: "NAME"` — the value itself is never written to the
trace. A literal value in the flow file is recorded (it is already plaintext in
your repository).

## Traces

A trace is evidence, and evidence can be sensitive: it holds page URLs, element
names and the actions taken. Traces are gitignored by default. Read one before
sharing it, and prefer `${ENV}` for anything you would not paste into a ticket.

## Your profile, your cookies

With `--user-data-dir` jevnav reads and writes a Chromium profile of your
choosing; with `--cdp` it acts inside the Chrome you already have open, with
everything that browser is logged into. Anything jevnav decides can then act as
you on those sites — that is the point, and it is also the risk. Point it at
your own accounts deliberately, and prefer a dedicated profile over your daily
one. jevnav only talks to the running instance over CDP; it never opens a
profile Chrome has locked, and it never closes a browser it attached to.

## Acting on a page

`browse`, `goal` and `run` execute actions only when the gate says `auto`.
`review` and `blocked` decisions are recorded and never acted on.
`replay --execute` re-resolves actions by fingerprint and refuses to act when the
target is gone or duplicated, so a shifted page cannot be clicked by position.

Fresh-browser mode (the default, no `--user-data-dir`/`--cdp`) starts a clean
profile with no stored cookies. `run`/`go` only visit the URLs in your flow;
under MCP the agent names its own URLs with `goto`/`new_page` — that is the point
of the tool — so point the server at sites you are willing to let it browse.

## MCP primitives: gated, traced, and where the boundary really is

The gate covers the calls where Jev chooses: `browse`, `goal`, and the `intent`
variants of `fill_form`/`upload_files`. Everything else is a direct primitive
that runs immediately — `press_key`, `fill_form`/`upload_files` with a
`selector`, `drag`, `route`, `read_js`, the artifact writers — because the
caller (the agent or the host behind it) already chose the exact target. Those
tools carry MCP annotations (`destructiveHint`, `openWorldHint`) so a host can
require confirmation; MCP annotations are hints, so a host that needs a hard
boundary must enforce it itself.

What the server does enforce:

- **Every acting call is recorded** in the session trace, with typed values,
  JS expressions and single-character keys masked (`kind: "action"`; decisions
  are `kind: "step"`) — unless the server was started with `--no-trace`.
  Observation tools are not traced, by design (see SPEC). Masking is textual:
  a modifier combination such as `Shift+a` is recorded as given, and an error
  message from the page can quote what was typed.
- **Files stay in one root.** `upload_files` reads only inside `--file-root`
  (default: the working directory), and `screenshot`, `heap_snapshot` and
  `trace_stop` write only inside it.
- **URLs are http(s).** `goto`/`new_page` refuse `file://` unless the server is
  started with `--allow-file-urls`; `lighthouse` follows the same rule and pins
  its npm version.
- **`read_js` is a read channel.** It runs arbitrary JavaScript in the page and
  returns the value — with `--cdp` attached to your own Chrome it can read
  anything that browser is logged into. Start the server with `--no-eval` to
  close it; the rest of jevnav works unchanged.

## Scope

jevnav drives a browser and can therefore do anything a browser can do on the
sites you point it at. Do not point it at systems you are not authorised to
automate, and do not run untrusted flows: a flow file is code-shaped input.
