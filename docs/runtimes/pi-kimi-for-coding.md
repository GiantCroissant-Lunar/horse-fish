# Pi CLI + Kimi For Coding Runtime

## Overview

Pi coding agent (`@mariozechner/pi-coding-agent`) configured with Kimi's coding-optimized model via custom provider in `~/.pi/agent/models.json`.

## Prerequisites

- `npm install -g @mariozechner/pi-coding-agent`
- Valid `KIMI_API_KEY` (from [Kimi platform](https://platform.kimi.com))

## Pi Models Config

File: `~/.pi/agent/models.json`

```json
{
  "providers": {
    "kimi-coding": {
      "baseUrl": "https://api.kimi.com/coding/v1",
      "api": "openai-completions",
      "apiKey": "KIMI_API_KEY",
      "headers": {
        "User-Agent": "KimiCLI/1.12.0"
      },
      "models": [
        {
          "id": "kimi-for-coding",
          "name": "Kimi For Coding",
          "reasoning": true,
          "input": ["text", "image"],
          "contextWindow": 262144,
          "maxTokens": 32768,
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          },
          "compat": {
            "supportsDeveloperRole": false,
            "supportsStore": false
          }
        }
      ],
      "authHeader": true
    }
  }
}
```

### Key compat settings

| Setting | Value | Reason |
|---------|-------|--------|
| `supportsDeveloperRole` | `false` | Kimi API rejects `developer` role messages |
| `supportsStore` | `false` | Kimi API rejects the `store` parameter |
| `authHeader` | `true` | Send API key as `Authorization: Bearer` header |
| `User-Agent` | `KimiCLI/1.12.0` | Kimi API whitelists coding agent user-agents |

## Overstory Config

File: `.overstory/config.yaml`

```yaml
runtime:
  pi:
    provider: kimi-coding
    model: kimi-for-coding
    modelMap:
      opus: kimi-coding/kimi-for-coding
      sonnet: kimi-coding/kimi-for-coding
      haiku: kimi-coding/kimi-for-coding
```

## Environment Setup

Pi reads `KIMI_API_KEY` from the environment (referenced by `"apiKey": "KIMI_API_KEY"` in models.json). Overstory spawns agents in tmux sessions that don't source `~/.zshrc`, so the key must be set via one of:

### Option 1: tmux global environment (recommended for overstory)

```bash
tmux set-environment -g KIMI_API_KEY sk-kimi-...
```

Set this before spawning any pi agents. Persists for the lifetime of the tmux server.

### Option 2: ~/.zshrc export

```bash
export KIMI_API_KEY=sk-kimi-...
```

Works when tmux sessions are started as login shells, but overstory's inline command pattern may bypass `.zshrc`.

## Spawning via Overstory

```bash
# Set env first
tmux set-environment -g KIMI_API_KEY "$KIMI_API_KEY"

# Spawn a builder
ov sling <task-id> --capability builder --runtime pi --name my-builder
```

## Validation

Tested 2026-03-08:
- Pi + kimi-for-coding successfully wrote files, committed to worktree branch, sent worker_done mail, and closed task
- ~55s for a simple file creation task
- No "no diff" issue — interactive tmux session ensures file writes happen
