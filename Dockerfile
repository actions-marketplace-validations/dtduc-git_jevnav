# Stdio MCP server for Glama's sandbox introspection (initialize + tools/list)
# and for anyone who wants jevnav mcp in a container.
#
# --no-trace: a session trace would be written to the cwd at startup; sandboxes
# with a read-only rootfs would fail there. Drop the flag if you want traces.
# Tool schemas need no browser: add
#   RUN playwright install --with-deps chromium
# if this image must also drive pages (e.g. Glama's "Try in browser").
FROM python:3.12-slim

RUN pip install --no-cache-dir jevnav

ENTRYPOINT ["jevnav", "mcp", "--no-trace"]
