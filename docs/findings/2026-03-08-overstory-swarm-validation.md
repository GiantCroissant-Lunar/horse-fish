# Overstory Swarm Validation — 2026-03-08

## Summary

Validated overstory CLI (v0.8.6) as a multi-agent development tool for building horse-fish. Tested 4 runtimes, ran a 4-agent parallel swarm to build the first foundation components.

## Runtime Validation

### Tested Runtimes

| Runtime | CLI Tool | Model | Result | File Write | Time (simple task) |
|---------|----------|-------|--------|------------|-------------------|
| Claude Code | `claude` | claude-sonnet-4.6 | Pass | Yes | ~44s |
| Pi | `pi` | kimi-for-coding | Pass | Yes | ~55s |
| Copilot | `copilot` | gpt-5.4 | Pass | Yes | ~2min |
| OpenCode | `opencode` | qwen3.5-plus | Pass | Yes | ~38s |

### Key Finding: "No Diff" Problem Eliminated

The core issue in tentacle-punch (agents responding with text instead of writing files, ~50% failure rate) does not exist with overstory. The difference: overstory runs agents in **interactive tmux sessions**, not piped subprocesses. Agents have full autonomy to read, write, and commit — same as a human using the tool.

## Setup Requirements

### Environment Variables

Each runtime needs specific env setup before spawning:

```bash
# Pi + Kimi: API key must be in tmux global environment
tmux set-environment -g KIMI_API_KEY "$KIMI_API_KEY"

# Copilot: needs full model name, not alias
export ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.4
# WARNING: This is global and breaks Claude Code (which rejects gpt-5.4)
# Don't set this when spawning claude runtime agents
```

### Copilot Folder Trust

Copilot shows a trust dialog for new directories. Overstory's adapter assumes no dialog exists. Pre-trust worktree paths:

```json
// ~/.copilot/config.json
{
  "trusted_folders": [
    "/path/to/project",
    "/path/to/project/.overstory/worktrees"
  ]
}
```

### Model Alias Problem

Overstory uses model aliases (`sonnet`, `opus`, `haiku`) in agent-manifest.json. Different runtimes need different model names:
- Claude Code: accepts `sonnet` natively
- Copilot: needs `gpt-5.4` or `claude-sonnet-4.6`
- Pi: needs `kimi-coding/kimi-for-coding`
- OpenCode: needs `opencode/qwen3.5-plus`

`ANTHROPIC_DEFAULT_SONNET_MODEL` env var controls alias expansion but is **global** — setting it for copilot breaks claude. Per-runtime model config via `config.yaml` runtime capabilities section is the proper fix.

## Swarm Run #1: Building Horse-Fish Foundation

### Tasks

4 independent foundation components built in parallel:

| Task | Agent | Runtime | Output | Tests | Time |
|------|-------|---------|--------|-------|------|
| Pydantic data models | models-claude2 | Claude Code | `models.py` (83L) | 20 tests | ~90s |
| SQLite store | store-claude | Claude Code | `store/db.py` (113L) | 10 tests | ~90s |
| Tmux agent manager | tmux-copilot | Copilot/gpt-5.4 | `tmux.py` (118L) + `runtime.py` (82L) | 8 tests | ~9min |
| Git worktree manager | worktree-pi | Pi/kimi-for-coding | `worktree.py` (298L) | 15 tests | ~4min |

**Total: 694 lines of source + 802 lines of tests, 53 tests all passing.**

### Merge Results

All 4 branches merged cleanly into main:
- 2 clean merges (store, worktree)
- 2 auto-resolved (models, tmux — conflicts only in `.mulch/mulch.config.yaml`)

### Issues Encountered

1. **Claude Code + `ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.4`**: Claude rejects gpt-5.4 as a model. First claude agent failed. Fixed by not setting the env var when spawning claude.

2. **OpenCode stalled waiting for coordinator**: OpenCode/qwen3.5-plus sent a "question" mail to coordinator and stopped working. No coordinator was running. Direct tmux nudge didn't help — model was unresponsive. Had to kill and re-sling with Claude.

3. **Copilot slow on mulch**: Copilot spent ~5 minutes exploring mulch infrastructure (README, config, directory structure) instead of focusing on the task. Actual code writing took ~3 minutes, mulch cleanup took ~6 minutes.

4. **Copilot folder trust in worktrees**: Each overstory worktree is a new directory path. Copilot's trust dialog caused agent crashes until worktree base dir was added to trusted_folders.

## Runtime Comparison (for development tasks)

| Aspect | Claude Code | Pi/Kimi | Copilot/GPT-5.4 | OpenCode/Qwen |
|--------|-------------|---------|-----------------|---------------|
| Speed | Fast (~90s) | Medium (~4min) | Slow (~9min) | Stalled |
| Reliability | High | High | Medium (mulch noise) | Low (waited for coordinator) |
| Code quality | Good | Good | Good | N/A |
| Setup complexity | Low | Medium (API key) | Medium (model + trust) | Medium (model) |
| Cost | Paid (Anthropic) | Free (Kimi) | Paid (GitHub) | Free (Alibaba) |
| Best for | All roles | Bulk building | Building (when working) | Needs investigation |

### Recommendations

1. **Use Claude Code as primary runtime** — fastest, most reliable, best instruction following
2. **Use Pi/Kimi as secondary** — free tier, good quality, reliable
3. **Use Copilot sparingly** — slow due to mulch exploration, model alias issues
4. **Investigate OpenCode** — stalling issue needs debugging before production use
5. **Fix model alias system** — need per-runtime model mapping, not global env var
