# Runtime Fixes Design — Copilot Env Isolation + Pi/Dashscope

Date: 2026-03-08

## Problems

### B: Copilot env var isolation
`ANTHROPIC_DEFAULT_SONNET_MODEL` is a global env var read by overstory's `expandAliasFromEnv()` at manifest resolution time. Setting it for copilot (e.g., `gpt-5.4`) also affects claude agents.

**Fix**: Set explicit model names in `.overstory/config.yaml` `models` section. This bypasses alias expansion entirely — `resolveModel()` checks `config.models[role]` before consulting env vars.

### C: OpenCode stalling
Overstory's `OpenCodeRuntime.detectReady()` is stubbed — always returns `{ phase: "loading" }`. The orchestrator never sends the beacon, so the agent sits idle.

**Fix**: Replace opencode with Pi CLI + Alibaba Coding Plan (dashscope) as the free runtime. Same models (glm-4.7, qwen3.5-plus), working overstory adapter.

## Changes

### 1. Pi CLI provider config (`~/.pi/agent/models.json`)
Add `dashscope` provider using OpenAI-compatible endpoint:
- Base URL: `https://coding-intl.dashscope.aliyuncs.com/v1`
- API format: `openai-completions`
- API key env var: `DASHSCOPE_API_KEY`
- Models: glm-4.7, qwen3.5-plus, kimi-k2.5

### 2. Environment
```bash
tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"
```

### 3. Overstory config (`.overstory/config.yaml`)
```yaml
models:
  builder: claude-sonnet-4-6
runtime:
  pi:
    provider: dashscope
    model: glm-4.7
    modelMap:
      opus: dashscope/glm-4.7
      sonnet: dashscope/glm-4.7
      haiku: dashscope/glm-4.7
```

### 4. Horse-fish RuntimeAdapter (`src/horse_fish/agents/runtime.py`)
Update `PiRuntime.build_env()` to pass `DASHSCOPE_API_KEY` from environment.

### 5. Documentation updates
- Update `docs/runtimes/pi-kimi-for-coding.md` to cover dashscope provider
- Update `docs/runtimes/opencode.md` to note it's disabled pending overstory fix
