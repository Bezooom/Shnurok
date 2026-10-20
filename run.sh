#!/usr/bin/env bash
# Launch iZOOM Web Console for iPad on Linux: http://127.0.0.1:8765
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

PYTHON="${PYTHON:-python3}"
if [ -d ".venv" ]; then
    PYTHON=".venv/bin/python"
elif [ -d "../Temp/ipad/.venv" ]; then
    PYTHON="../Temp/ipad/.venv/bin/python"
fi

echo "[*] Starting iZOOM console on http://127.0.0.1:8765..."
exec "${PYTHON}" -m uvicorn console.server:app --host 127.0.0.1 --port 8765
