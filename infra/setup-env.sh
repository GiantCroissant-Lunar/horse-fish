#!/usr/bin/env bash
# Load horse-fish secrets into the current shell and tmux global environment.
# Usage: source infra/setup-env.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: ${ENV_FILE} not found."
    echo "Run: cp infra/.env.example infra/.env  and fill in your keys."
    return 1 2>/dev/null || exit 1
fi

# Load .env (skip comments and blank lines)
while IFS='=' read -r key value; do
    # Skip comments and empty lines
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    # Strip surrounding quotes from value
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"

    if [[ -n "$value" ]]; then
        export "$key=$value"
        # Also set in tmux global env if tmux is running
        if command -v tmux &>/dev/null && tmux list-sessions &>/dev/null 2>&1; then
            tmux set-environment -g "$key" "$value" 2>/dev/null || true
        fi
        echo "  Loaded: $key"
    fi
done < "$ENV_FILE"

echo "Environment loaded from ${ENV_FILE}"
