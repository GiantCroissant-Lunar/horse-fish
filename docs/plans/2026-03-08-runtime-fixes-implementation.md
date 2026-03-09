# Runtime Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix copilot env var isolation and set up Pi CLI + Alibaba Coding Plan (dashscope) as a working free runtime, replacing opencode.

**Architecture:** Config changes to Pi CLI models, overstory config, and horse-fish runtime adapter. No new modules — just wiring existing pieces correctly.

**Tech Stack:** Pi CLI models.json, overstory config.yaml, Python runtime adapter

---

### Task 1: Add dashscope provider to Pi CLI models config

**Files:**
- Modify: `~/.pi/agent/models.json`

**Step 1: Add dashscope provider**

Add a new `dashscope` provider entry after the existing `zai` provider in `~/.pi/agent/models.json`:

```json
"dashscope": {
  "baseUrl": "https://coding-intl.dashscope.aliyuncs.com/v1",
  "api": "openai-completions",
  "apiKey": "DASHSCOPE_API_KEY",
  "authHeader": true,
  "models": [
    {
      "id": "glm-4.7",
      "name": "GLM-4.7",
      "reasoning": true,
      "input": ["text"],
      "contextWindow": 202752,
      "maxTokens": 16384,
      "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
    },
    {
      "id": "qwen3.5-plus",
      "name": "Qwen3.5 Plus",
      "reasoning": true,
      "input": ["text", "image"],
      "contextWindow": 1000000,
      "maxTokens": 65536,
      "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
    },
    {
      "id": "kimi-k2.5",
      "name": "Kimi K2.5",
      "reasoning": true,
      "input": ["text", "image"],
      "contextWindow": 262144,
      "maxTokens": 32768,
      "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
    }
  ]
}
```

**Step 2: Set DASHSCOPE_API_KEY in tmux global env**

Run: `tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"`

**Step 3: Verify Pi can see the new provider**

Run: `pi --provider dashscope --model glm-4.7 --print "say hello in one word"`
Expected: A response from glm-4.7 (any text output, no 401 error)

---

### Task 2: Update overstory config for env isolation + dashscope

**Files:**
- Modify: `.overstory/config.yaml`

**Step 1: Add explicit model names to bypass alias expansion**

In `.overstory/config.yaml`, update the `models:` section (currently empty) to:

```yaml
models:
  builder: claude-sonnet-4-6
  coordinator: claude-sonnet-4-6
```

This prevents `ANTHROPIC_DEFAULT_SONNET_MODEL` from being consulted — copilot agents get their model via `--runtime copilot` + the `ov sling` command, not alias expansion.

**Step 2: Update Pi runtime config to use dashscope**

Replace the existing `runtime.pi` section:

```yaml
runtime:
  default: claude
  shellInitDelayMs: 0
  pi:
    provider: dashscope
    model: glm-4.7
    modelMap:
      opus: dashscope/glm-4.7
      sonnet: dashscope/glm-4.7
      haiku: dashscope/glm-4.7
```

**Step 3: Verify overstory can sling a Pi agent**

Run: `sd create --title "Test dashscope" --description "Print hello world" --json`
Then: `ov sling <task-id> --capability builder --runtime pi --name dashscope-test`
Expected: Agent launches without 401 error. Check tmux pane for activity.
Cleanup: `ov clean --all`

---

### Task 3: Update horse-fish PiRuntime.build_env()

**Files:**
- Modify: `src/horse_fish/agents/runtime.py:50-60`
- Modify: `tests/test_tmux.py` (runtime registry test)

**Step 1: Write the failing test**

Add to `tests/test_tmux.py` (or a new `tests/test_runtime.py` if preferred):

```python
import os
from unittest.mock import patch

from horse_fish.agents.runtime import PiRuntime


def test_pi_runtime_build_env_includes_dashscope_key():
    with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-sp-test123"}):
        runtime = PiRuntime()
        env = runtime.build_env()
        assert env["DASHSCOPE_API_KEY"] == "sk-sp-test123"


def test_pi_runtime_build_env_empty_when_no_key():
    with patch.dict(os.environ, {}, clear=True):
        runtime = PiRuntime()
        env = runtime.build_env()
        assert "DASHSCOPE_API_KEY" not in env
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_tmux.py::test_pi_runtime_build_env_includes_dashscope_key -v`
Expected: FAIL — current `build_env()` returns `{}`

**Step 3: Update PiRuntime.build_env()**

In `src/horse_fish/agents/runtime.py`, update `PiRuntime`:

```python
import os

# ... existing imports ...

@dataclass(frozen=True, slots=True)
class PiRuntime:
    """Adapter for the Pi CLI."""

    runtime_id: ClassVar[str] = "pi"

    def build_spawn_command(self, model: str) -> str:
        return f"pi --model {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in ("DASHSCOPE_API_KEY", "KIMI_API_KEY", "ZAI_API_KEY"):
            val = os.environ.get(key)
            if val:
                env[key] = val
        return env
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/ -v`
Expected: All 111+ tests pass, including the 2 new ones

**Step 5: Commit**

```bash
git add src/horse_fish/agents/runtime.py tests/test_tmux.py
git commit -m "feat: PiRuntime.build_env() passes API keys to tmux sessions"
```

---

### Task 4: Update documentation

**Files:**
- Modify: `docs/runtimes/pi-kimi-for-coding.md`
- Modify: `docs/runtimes/opencode.md`

**Step 1: Update Pi runtime docs**

Add a dashscope section to `docs/runtimes/pi-kimi-for-coding.md` documenting:
- Dashscope provider config in `~/.pi/agent/models.json`
- `DASHSCOPE_API_KEY` env var setup
- Available models: glm-4.7, qwen3.5-plus, kimi-k2.5
- Overstory config for dashscope

**Step 2: Update OpenCode runtime docs**

Add a note to `docs/runtimes/opencode.md`:
- OpenCode is disabled as an overstory runtime pending `detectReady` fix
- Use Pi CLI + dashscope for the same models (glm-4.7, qwen3.5-plus)

**Step 3: Commit**

```bash
git add docs/runtimes/
git commit -m "docs: update runtime docs for dashscope provider, disable opencode"
```

---

### Task 5: Smoke test end-to-end

**Step 1: Sling a Pi/dashscope agent on a real task**

```bash
tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"
sd create --title "Test Pi dashscope e2e" --description "Create a file test_smoke.py that prints hello" --json
ov sling <task-id> --capability builder --runtime pi --name smoke-test
ov status  # wait for completion
```

Expected: Agent completes, creates file, sends worker_done mail.

**Step 2: Sling a Claude agent to confirm no regression**

```bash
sd create --title "Test Claude no regression" --description "Create a file test_claude_smoke.py that prints hello" --json
ov sling <task-id> --capability builder --runtime claude --name claude-smoke
ov status  # wait for completion
```

Expected: Claude agent works as before, unaffected by config changes.

**Step 3: Cleanup**

```bash
ov clean --all
```
