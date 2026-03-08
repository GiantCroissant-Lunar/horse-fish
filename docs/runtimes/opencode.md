# OpenCode Runtime

## Overview

OpenCode CLI (`opencode-ai`) used as an overstory agent runtime. Connects to Alibaba Coding Plan models via custom provider config.

## Prerequisites

- `npm install -g opencode-ai`
- Config: `~/.config/opencode/opencode.json` (provider + model definitions)
- Auth: `~/.local/share/opencode/auth.json` (API key for provider)

## Provider Config

File: `~/.config/opencode/opencode.json`

The Alibaba Coding Plan provides 7 models via a single `sk-sp-xxx` subscription key:
- qwen3.5-plus, qwen3-coder-plus, qwen3-coder-next
- glm-4.7, glm-5
- kimi-k2.5, MiniMax-M2.5

Endpoint: `https://coding-intl.dashscope.aliyuncs.com/apps/anthropic/v1` (Anthropic-compatible)

## Overstory Compatibility

### Model alias expansion

Same issue as Copilot — overstory uses aliases like `sonnet`. OpenCode needs `provider/model` format.

**Fix**: Set env var before spawning:

```bash
export ANTHROPIC_DEFAULT_SONNET_MODEL=opencode/qwen3.5-plus
```

### Adapter status

The overstory OpenCode adapter (`src/runtimes/opencode.ts`) is partially stubbed:
- `buildSpawnCommand`: Works — `opencode --model <model>`
- `detectReady`: Stubbed (returns "loading" always, but TUI detection works via timeout fallback)
- `parseTranscript`: Returns null (no token tracking yet)
- `getTranscriptDir`: Not implemented

Despite stubs, the runtime works for interactive tmux sessions.

## Spawning via Overstory

```bash
export ANTHROPIC_DEFAULT_SONNET_MODEL=opencode/qwen3.5-plus

ov sling <task-id> --capability builder --runtime opencode --name my-builder
```

## Validation

Tested 2026-03-08:
- OpenCode + qwen3.5-plus successfully wrote files, committed to worktree branch, recorded mulch learnings, sent worker_done mail, and closed task
- ~38s for a simple file creation task (fastest of all runtimes tested)
- No "no diff" issue — interactive tmux session ensures file writes happen

## Notes

- OpenCode uses `AGENTS.md` for instruction overlay
- No hooks deployment — OpenCode has no hook mechanism
- Interactive TUI mode with `-m provider/model` flag
