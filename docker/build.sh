#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -t autodev-sandbox:latest "$SCRIPT_DIR"
echo "Built autodev-sandbox:latest successfully."
