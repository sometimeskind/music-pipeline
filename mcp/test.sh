#!/bin/sh
# The MCP adapter's tests, contract test included: the adapter in its own venv, the real
# pipeline API (service/tests/mcp_harness.py) run by $SERVICE_PYTHON, which must have the
# service installed. Run by CI and by `just test-mcp` (inside the service dev image).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
venv=${MCP_VENV:-/tmp/music-mcp-venv}
python3 -m venv "$venv"
"$venv/bin/pip" install -q -r "$here/requirements.txt" -r "$here/requirements-dev.txt"
cd "$here"
MUSIC_MCP_CONTRACT_PYTHON=${SERVICE_PYTHON:-python3} MUSIC_MCP_CONTRACT_REQUIRED=1 \
  exec "$venv/bin/python" -m pytest -p no:cacheprovider tests/ "$@"
