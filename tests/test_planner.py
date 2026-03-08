"""Tests for the Planner task decomposition module."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.models import Subtask, SubtaskState
from horse_fish.planner import Planner, PlannerError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_process(stdout: bytes, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


SAMPLE_SUBTASKS = [
    {"description": "Set up database schema", "deps": [], "files_hint": ["src/db.py"]},
    {
        "description": "Implement API endpoints",
        "deps": ["Set up database schema"],
        "files_hint": ["src/api.py", "src/routes.py"],
    },
    {
        "description": "Write integration tests",
        "deps": ["Implement API endpoints"],
        "files_hint": ["tests/test_api.py"],
    },
]


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def test_build_prompt_contains_task():
    planner = Planner(runtime="claude")
    prompt = planner._build_prompt("Add user authentication", "")
    assert "Add user authentication" in prompt


def test_build_prompt_contains_context():
    planner = Planner(runtime="claude")
    prompt = planner._build_prompt("some task", "Django project, PostgreSQL backend")
    assert "Django project, PostgreSQL backend" in prompt


def test_build_prompt_fallback_context():
    planner = Planner(runtime="claude")
    prompt = planner._build_prompt("task", "")
    assert "No additional context provided." in prompt


def test_build_prompt_instructs_json():
    planner = Planner(runtime="claude")
    prompt = planner._build_prompt("task", "")
    assert "JSON" in prompt
    assert "description" in prompt
    assert "deps" in prompt
    assert "files_hint" in prompt


# ---------------------------------------------------------------------------
# Command building per runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "runtime,expected_parts",
    [
        ("claude", ["claude", "--print", "-m"]),
        ("copilot", ["copilot", "--print", "--model"]),
        ("pi", ["pi", "--print", "--model"]),
        ("opencode", ["opencode", "--print", "-m"]),
    ],
)
def test_build_command_includes_runtime_flags(runtime: str, expected_parts: list[str]):
    planner = Planner(runtime=runtime)
    cmd = planner._build_command("some prompt")
    for part in expected_parts:
        assert part in cmd


def test_build_command_includes_model():
    planner = Planner(runtime="claude", model="claude-opus-4-6")
    cmd = planner._build_command("prompt")
    assert "claude-opus-4-6" in cmd


def test_build_command_includes_prompt():
    planner = Planner(runtime="claude")
    cmd = planner._build_command("my custom prompt text")
    assert "my custom prompt text" in cmd


def test_invalid_runtime_raises():
    with pytest.raises(ValueError, match="Unknown runtime"):
        Planner(runtime="unknown-runtime")


# ---------------------------------------------------------------------------
# JSON parsing — valid response
# ---------------------------------------------------------------------------


def test_parse_response_valid_json():
    planner = Planner(runtime="claude")
    raw = json.dumps(SAMPLE_SUBTASKS)
    subtasks = planner._parse_response(raw)
    assert len(subtasks) == 3
    assert all(isinstance(s, Subtask) for s in subtasks)


def test_parse_response_preserves_description():
    planner = Planner(runtime="claude")
    raw = json.dumps(SAMPLE_SUBTASKS)
    subtasks = planner._parse_response(raw)
    descriptions = [s.description for s in subtasks]
    assert "Set up database schema" in descriptions
    assert "Implement API endpoints" in descriptions


def test_parse_response_preserves_deps():
    planner = Planner(runtime="claude")
    raw = json.dumps(SAMPLE_SUBTASKS)
    subtasks = planner._parse_response(raw)
    api_task = next(s for s in subtasks if "API" in s.description)
    assert "Set up database schema" in api_task.deps


def test_parse_response_preserves_files_hint():
    planner = Planner(runtime="claude")
    raw = json.dumps(SAMPLE_SUBTASKS)
    subtasks = planner._parse_response(raw)
    db_task = next(s for s in subtasks if "database" in s.description)
    assert "src/db.py" in db_task.files_hint


def test_parse_response_assigns_unique_ids():
    planner = Planner(runtime="claude")
    raw = json.dumps(SAMPLE_SUBTASKS)
    subtasks = planner._parse_response(raw)
    ids = [s.id for s in subtasks]
    assert len(ids) == len(set(ids)), "All subtask IDs must be unique"


def test_parse_response_default_state():
    planner = Planner(runtime="claude")
    raw = json.dumps(SAMPLE_SUBTASKS)
    subtasks = planner._parse_response(raw)
    assert all(s.state == SubtaskState.pending for s in subtasks)


def test_parse_response_missing_optional_fields():
    planner = Planner(runtime="claude")
    raw = json.dumps([{"description": "Simple task"}])
    subtasks = planner._parse_response(raw)
    assert subtasks[0].deps == []
    assert subtasks[0].files_hint == []


# ---------------------------------------------------------------------------
# JSON parsing — markdown code fences
# ---------------------------------------------------------------------------


def test_parse_response_strips_json_code_fence():
    planner = Planner(runtime="claude")
    raw = "```json\n" + json.dumps(SAMPLE_SUBTASKS) + "\n```"
    subtasks = planner._parse_response(raw)
    assert len(subtasks) == 3


def test_parse_response_strips_plain_code_fence():
    planner = Planner(runtime="claude")
    raw = "```\n" + json.dumps(SAMPLE_SUBTASKS) + "\n```"
    subtasks = planner._parse_response(raw)
    assert len(subtasks) == 3


def test_parse_response_fence_with_preamble():
    planner = Planner(runtime="claude")
    raw = "Here are the subtasks:\n```json\n" + json.dumps([{"description": "Task A"}]) + "\n```\n"
    subtasks = planner._parse_response(raw)
    assert len(subtasks) == 1


# ---------------------------------------------------------------------------
# JSON parsing — error cases
# ---------------------------------------------------------------------------


def test_parse_response_empty_raises():
    planner = Planner(runtime="claude")
    with pytest.raises(PlannerError, match="Empty response"):
        planner._parse_response("")


def test_parse_response_malformed_json_raises():
    planner = Planner(runtime="claude")
    with pytest.raises(PlannerError, match="Failed to parse JSON"):
        planner._parse_response("this is not json {{{ invalid")


def test_parse_response_non_array_raises():
    planner = Planner(runtime="claude")
    with pytest.raises(PlannerError, match="Expected JSON array"):
        planner._parse_response('{"description": "not an array"}')


def test_parse_response_missing_description_raises():
    planner = Planner(runtime="claude")
    raw = json.dumps([{"deps": [], "files_hint": []}])
    with pytest.raises(PlannerError, match="missing valid 'description'"):
        planner._parse_response(raw)


def test_parse_response_empty_array_returns_empty():
    planner = Planner(runtime="claude")
    subtasks = planner._parse_response("[]")
    assert subtasks == []


# ---------------------------------------------------------------------------
# Full decompose() via mocked subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_calls_subprocess():
    planner = Planner(runtime="claude", model="claude-sonnet-4-6")
    mock_proc = make_mock_process(json.dumps(SAMPLE_SUBTASKS).encode())

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)) as mock_exec:
        subtasks = await planner.decompose("Build a REST API")

    mock_exec.assert_awaited_once()
    assert len(subtasks) == 3


@pytest.mark.asyncio
async def test_decompose_passes_model_in_command():
    planner = Planner(runtime="claude", model="claude-opus-4-6")
    mock_proc = make_mock_process(json.dumps([{"description": "Task"}]).encode())

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)) as mock_exec:
        await planner.decompose("task")

    call_args = mock_exec.call_args[0]
    assert "claude-opus-4-6" in call_args


@pytest.mark.asyncio
async def test_decompose_cli_failure_raises():
    planner = Planner(runtime="claude")
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"CLI error occurred"))

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        with pytest.raises(PlannerError, match="exited with code 1"):
            await planner.decompose("task")


@pytest.mark.asyncio
async def test_decompose_returns_subtask_objects():
    planner = Planner(runtime="claude")
    payload = [
        {"description": "Step 1", "deps": [], "files_hint": ["src/step1.py"]},
        {"description": "Step 2", "deps": ["Step 1"], "files_hint": []},
    ]
    mock_proc = make_mock_process(json.dumps(payload).encode())

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
        subtasks = await planner.decompose("two step task", context="Python project")

    assert len(subtasks) == 2
    assert subtasks[0].description == "Step 1"
    assert subtasks[1].deps == ["Step 1"]
