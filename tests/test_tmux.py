"""Tests for tmux session management and runtime adapters."""

from __future__ import annotations

import asyncio

import pytest

from horse_fish.agents.runtime import RUNTIME_REGISTRY
from horse_fish.agents.tmux import TmuxManager


class FakeProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()


def fake_exec_factory(processes: list[FakeProcess], calls: list[tuple[tuple[object, ...], dict[str, object]]]):
    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        calls.append((args, kwargs))
        return processes.pop(0)

    return fake_exec


@pytest.mark.asyncio
async def test_spawn_starts_tmux_session_and_returns_pane_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    processes = [FakeProcess(), FakeProcess(stdout="4321\n")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    manager = TmuxManager()
    pane_pid = await manager.spawn(
        name="agent-1",
        command="copilot --model gpt-5.4 --allow-all-tools",
        cwd="/tmp/worktree",
        env={"BETA": "two words", "ALPHA": "1"},
    )

    assert pane_pid == 4321
    assert calls[0][0] == (
        "tmux",
        "new-session",
        "-d",
        "-s",
        "agent-1",
        "-c",
        "/tmp/worktree",
        "export ALPHA=1 && export BETA='two words' && copilot --model gpt-5.4 --allow-all-tools",
    )
    assert calls[1][0] == ("tmux", "list-panes", "-t", "agent-1", "-F", "#{pane_pid}")
    assert calls[0][1]["stdout"] is asyncio.subprocess.PIPE
    assert calls[0][1]["stderr"] is asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_spawn_raises_for_duplicate_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    processes = [FakeProcess(returncode=1, stderr="duplicate session: agent-1")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    manager = TmuxManager()

    with pytest.raises(RuntimeError, match="duplicate session"):
        await manager.spawn("agent-1", "claude", "/tmp/worktree")


@pytest.mark.asyncio
async def test_send_keys_sends_text_then_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    processes = [FakeProcess(), FakeProcess()]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    manager = TmuxManager()
    await manager.send_keys("agent-1", "hf run")

    assert calls[0][0] == ("tmux", "send-keys", "-t", "agent-1", "-l", "hf run")
    assert calls[1][0] == ("tmux", "send-keys", "-t", "agent-1", "Enter")


@pytest.mark.asyncio
async def test_capture_pane_returns_none_for_dead_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    processes = [FakeProcess(returncode=1, stderr="can't find pane: missing")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    manager = TmuxManager()

    assert await manager.capture_pane("missing") is None


@pytest.mark.asyncio
async def test_kill_session_ignores_missing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    processes = [FakeProcess(returncode=1, stderr="can't find session: missing")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    manager = TmuxManager()
    await manager.kill_session("missing")

    assert calls[0][0] == ("tmux", "kill-session", "-t", "missing")


@pytest.mark.asyncio
async def test_list_sessions_returns_empty_list_when_server_is_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    processes = [FakeProcess(returncode=1, stderr="no server running on /tmp/tmux-1000/default")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    manager = TmuxManager()

    assert await manager.list_sessions() == []


@pytest.mark.asyncio
async def test_is_alive_checks_existing_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list_sessions() -> list[str]:
        return ["agent-1", "agent-2"]

    manager = TmuxManager()
    monkeypatch.setattr(manager, "list_sessions", fake_list_sessions)

    assert await manager.is_alive("agent-1") is True
    assert await manager.is_alive("missing") is False


def test_runtime_registry_contains_supported_runtimes() -> None:
    assert set(RUNTIME_REGISTRY) == {"claude", "copilot", "pi", "opencode", "kimi", "droid", "bash"}
    assert RUNTIME_REGISTRY["claude"].build_spawn_command("sonnet") == "claude --model sonnet"
    assert RUNTIME_REGISTRY["copilot"].build_spawn_command("gpt-5.4") == "copilot --model gpt-5.4 --allow-all-tools"
    assert RUNTIME_REGISTRY["pi"].build_spawn_command("qwen3.5-plus") == "pi --provider dashscope --model qwen3.5-plus"
    assert RUNTIME_REGISTRY["opencode"].build_spawn_command("qwen3.5-plus") == "opencode -m qwen3.5-plus"
    assert RUNTIME_REGISTRY["claude"].build_env() == {}
