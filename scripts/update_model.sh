#!/usr/bin/env bash
# Update the default model used by AutoDev.
# Usage: ./scripts/update_model.sh <model-name>
# Example: ./scripts/update_model.sh qwen2.5-coder:14b

set -euo pipefail

CONFIG="config.yaml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_PATH="$PROJECT_DIR/$CONFIG"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <model-name>"
    echo "Example: $0 qwen2.5-coder:14b"
    exit 1
fi

NEW_MODEL="$1"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "Error: $CONFIG_PATH not found."
    exit 1
fi

echo "=== AutoDev Model Update ==="
echo ""

# Step 1: Pull the model
echo "[1/3] Pulling model '$NEW_MODEL' via Ollama..."
if ! ollama pull "$NEW_MODEL"; then
    echo "Error: Failed to pull model '$NEW_MODEL'."
    echo "Check the model name and your internet connection."
    exit 1
fi
echo "     Done."
echo ""

# Step 2: Update config.yaml
OLD_MODEL=$(grep -E '^\s+default:' "$CONFIG_PATH" | head -1 | sed 's/.*default:\s*"\?\([^"]*\)"\?/\1/' | xargs)
echo "[2/3] Updating $CONFIG..."
echo "     Old model: $OLD_MODEL"
echo "     New model: $NEW_MODEL"

sed -i "s|^\(\s*default:\s*\).*|\1\"$NEW_MODEL\"|" "$CONFIG_PATH"

# Verify the change
VERIFY=$(grep -E '^\s+default:' "$CONFIG_PATH" | head -1)
echo "     Verified:  $VERIFY"
echo ""

# Step 3: Check for hardcoded model names in src/
echo "[3/3] Checking for hardcoded model names in src/..."
if grep -rniE "(qwen|llama|codellama|deepseek|mistral|phi)" "$PROJECT_DIR/src/" --include="*.py" | grep -v "ollama" | grep -v "^Binary"; then
    echo "Warning: Possible hardcoded model names found in src/."
else
    echo "     Clean — no hardcoded model names in src/."
fi

echo ""
echo "=== Update complete ==="
echo "Model changed: $OLD_MODEL -> $NEW_MODEL"
echo "Run 'make eval' to benchmark the new model."
