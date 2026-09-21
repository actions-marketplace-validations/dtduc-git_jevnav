# Cases excluded after hand review

Ground truth only counts when it is unambiguous. These were written, run, then removed:

- **pydocs-search**: two visible search inputs (desktop + mobile) — an ambiguous target
- **pydocs-whatsnew**: the page offers both a version-specific link and an index; Jev chose the version-specific one, which is arguably the better answer — the label was mine and it was ambiguous

Other invalid cases (dropped earlier) had `expect` selectors that matched zero or
several visible elements: wiki-main-random, wiki-art-edit, wiki-art-lang,
pypi-requests-json, python-download, npm-search, so-search, mdn-home-css,
mdn-home-a11y, wiki-main-vietnamese, wiki-art-history, pypi-home-help,
pypi-home-guide, pypi-requests-homepage, pypi-requests-repo, gh-repo-license,
example-home, python-docs, python-events, gh-repo-* (pre-selector fixes).
