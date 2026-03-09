"""Comprehensive tests for runtime adapters."""

import os
import re
from unittest.mock import patch

from horse_fish.agents.runtime import (
    RUNTIME_REGISTRY,
    BashRuntime,
    ClaudeRuntime,
    CopilotRuntime,
    OpenCodeRuntime,
    PiRuntime,
)


class TestRuntimeRegistry:
    """Tests for runtime registry and common attributes."""

    def test_all_runtimes_have_ready_pattern(self) -> None:
        """All runtimes in registry must have a non-empty ready_pattern."""
        for runtime_id, runtime in RUNTIME_REGISTRY.items():
            assert hasattr(runtime, "ready_pattern"), f"{runtime_id} missing ready_pattern"
            assert runtime.ready_pattern, f"{runtime_id} has empty ready_pattern"

    def test_all_runtimes_have_ready_timeout(self) -> None:
        """All runtimes in registry must have positive ready_timeout_seconds."""
        for runtime_id, runtime in RUNTIME_REGISTRY.items():
            assert hasattr(runtime, "ready_timeout_seconds"), f"{runtime_id} missing ready_timeout_seconds"
            assert runtime.ready_timeout_seconds > 0, f"{runtime_id} has non-positive timeout"


class TestClaudeRuntime:
    """Tests for Claude runtime adapter."""

    def test_claude_spawn_command_includes_model(self) -> None:
        """Claude spawn command includes 'claude' and model name."""
        runtime = ClaudeRuntime()
        command = runtime.build_spawn_command("claude-sonnet-4-6")
        assert "claude" in command
        assert "claude-sonnet-4-6" in command

    def test_claude_spawn_command_no_model(self) -> None:
        """Claude spawn command without model returns just 'claude'."""
        runtime = ClaudeRuntime()
        command = runtime.build_spawn_command("")
        assert command == "claude"

    def test_claude_ready_pattern_matches_prompt(self) -> None:
        """Claude ready_pattern matches expected prompt markers."""
        pattern = re.compile(ClaudeRuntime.ready_pattern, re.MULTILINE)
        # Should match prompt markers
        assert pattern.search("Welcome to Claude\n❯ ") is not None
        assert pattern.search("shift+tab to accept") is not None
        assert pattern.search("bypass permissions") is not None
        # Should not match loading messages
        assert pattern.search("Loading...") is None


class TestPiRuntime:
    """Tests for Pi runtime adapter."""

    def test_pi_build_env_passes_dashscope_key(self) -> None:
        """Pi build_env returns DASHSCOPE_API_KEY when present in environment."""
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key-123"}):
            runtime = PiRuntime()
            env = runtime.build_env()
            assert "DASHSCOPE_API_KEY" in env
            assert env["DASHSCOPE_API_KEY"] == "test-key-123"

    def test_pi_build_env_empty_when_no_key(self) -> None:
        """Pi build_env returns empty dict when DASHSCOPE_API_KEY is not set."""
        with patch.dict(os.environ, {}, clear=True):
            runtime = PiRuntime()
            env = runtime.build_env()
            assert env == {}

    def test_pi_spawn_command(self) -> None:
        """Pi spawn command includes 'pi' and model name."""
        runtime = PiRuntime()
        command = runtime.build_spawn_command("qwen3.5-plus")
        assert "pi" in command
        assert "qwen3.5-plus" in command

    def test_pi_ready_pattern_matches_prompt(self) -> None:
        """Pi ready_pattern matches status bar token indicator."""
        pattern = re.compile(PiRuntime.ready_pattern, re.MULTILINE)
        # Should match Pi's status bar format
        assert pattern.search("0.0%/1.0M (auto)  (dashscope) qwen3.5-plus") is not None
        assert pattern.search("42.5%/200k done") is not None
        # Should not match loading messages
        assert pattern.search("Loading Pi...") is None


class TestCopilotRuntime:
    """Tests for Copilot runtime adapter."""

    def test_copilot_spawn_command(self) -> None:
        """Copilot spawn command includes 'copilot' and '--allow-all-tools'."""
        runtime = CopilotRuntime()
        command = runtime.build_spawn_command("gpt-5.4")
        assert "copilot" in command
        assert "--allow-all-tools" in command
        assert "gpt-5.4" in command


class TestOpenCodeRuntime:
    """Tests for OpenCode runtime adapter."""

    def test_opencode_spawn_command(self) -> None:
        """OpenCode spawn command includes 'opencode' and '-m'."""
        runtime = OpenCodeRuntime()
        command = runtime.build_spawn_command("qwen3.5-plus")
        assert "opencode" in command
        assert "-m" in command
        assert "qwen3.5-plus" in command


class TestBashRuntime:
    """Tests for Bash runtime adapter."""

    def test_bash_spawn_command(self) -> None:
        """Bash spawn command returns 'bash'."""
        runtime = BashRuntime()
        command = runtime.build_spawn_command("any-model")
        assert command == "bash"

    def test_bash_build_env_empty(self) -> None:
        """Bash build_env returns empty dict."""
        runtime = BashRuntime()
        env = runtime.build_env()
        assert env == {}

    def test_bash_in_runtime_registry(self) -> None:
        """Bash runtime is registered in RUNTIME_REGISTRY."""
        assert "bash" in RUNTIME_REGISTRY
        assert isinstance(RUNTIME_REGISTRY["bash"], BashRuntime)
