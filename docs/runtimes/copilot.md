# GitHub Copilot CLI Runtime

## Overview

GitHub Copilot CLI used as an overstory agent runtime. Authenticates via GitHub OAuth (no API key needed).

## Prerequisites

- `npm install -g @anthropic-ai/copilot-cli` or via GitHub
- `copilot login` (one-time GitHub OAuth)
- GitHub Copilot subscription

## Overstory Compatibility Issues

### 1. Model alias expansion

Overstory's agent manifest uses aliases like `sonnet`, `opus`, `haiku`. Copilot rejects these — it requires full model names:

```
claude-sonnet-4.6, claude-opus-4.6, gpt-5.4, gpt-5.3-codex,
gemini-3-pro-preview, gpt-5.2, gpt-5.1-codex, etc.
```

**Fix**: Set `ANTHROPIC_DEFAULT_SONNET_MODEL` env var before spawning:

```bash
export ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.4
```

This makes overstory's alias expansion return `gpt-5.4` instead of `sonnet`.

### 2. Folder trust dialog

Copilot shows a folder trust dialog for new directories. Overstory's copilot adapter assumes no trust dialog exists, so the agent crashes before overstory can send the beacon.

**Fix**: Pre-trust the worktree base directory in `~/.copilot/config.json`:

```json
{
  "trusted_folders": [
    "/path/to/project",
    "/path/to/project/.overstory/worktrees"
  ]
}
```

Or trust the folder manually once via copilot interactive mode (option 2: "Yes, and remember").

## Spawning via Overstory

```bash
# Set model alias
export ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.4

# Spawn a builder
ov sling <task-id> --capability builder --runtime copilot --name my-builder
```

## Validation

Tested 2026-03-08:
- Copilot + gpt-5.4 successfully wrote files, committed to worktree branch, ran quality gates, recorded mulch learnings, sent worker_done mail, and closed task
- ~2 min for a simple file creation task (slower than claude/pi due to exploring mulch infrastructure)
- No "no diff" issue — interactive tmux session ensures file writes happen

## Notes

- Copilot uses `.github/copilot-instructions.md` for instruction overlay (not `.claude/CLAUDE.md`)
- No hooks deployment — Copilot has no hook mechanism
- `--allow-all-tools` flag used for bypass permissions mode
