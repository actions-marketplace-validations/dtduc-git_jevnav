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

`browse` and `run` execute actions only when the gate says `auto`. `review` and
`blocked` decisions are recorded and never acted on. `replay --execute`
re-resolves actions by fingerprint and refuses to act when the target is gone or
duplicated, so a shifted page cannot be clicked by position.

The browser is launched fresh per run (a clean profile, no stored cookies) and
is never pointed at a site you did not put in a flow or an MCP session.

## Scope

jevnav drives a browser and can therefore do anything a browser can do on the
sites you point it at. Do not point it at systems you are not authorised to
automate, and do not run untrusted flows: a flow file is code-shaped input.
