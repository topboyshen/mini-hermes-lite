#!/bin/bash
# mini-hermes 测试统一入口（与 DEPLOYMENT.md / vps-preflight 一致）
set -euo pipefail
cd "$(dirname "$0")"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=app .venv/bin/python -m unittest discover -s tests "$@"
