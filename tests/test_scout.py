"""Tests for the scout phase — codebase context gathering for enriched planning."""

from __future__ import annotations

from pathlib import Path

import pytest

from horse_fish.models import ContextBrief, FileContext
from horse_fish.planner.scout import (
    SCOUT_PROMPT_TEMPLATE,
    _extract_patterns,
    _infer_purpose,
    _scan_source_files,
    format_brief_for_prompt,
    parse_scout_output,
    programmatic_scout,
)


class TestContextBriefModel:
    """Tests for the ContextBrief and FileContext Pydantic models."""

    def test_empty_brief(self):
        brief = ContextBrief()
        assert brief.relevant_files == []
        assert brief.patterns == []
        assert brief.dependencies == []
        assert brief.acceptance_criteria == []
        assert brief.risks == []
        assert brief.suggested_approach == ""

    def test_file_context_basic(self):
        fc = FileContext(path="src/main.py", purpose="Entry point")
        assert fc.path == "src/main.py"
        assert fc.purpose == "Entry point"
        assert fc.line_count is None

    def test_file_context_with_line_count(self):
        fc = FileContext(path="src/main.py", purpose="Entry point", line_count=42)
        assert fc.line_count == 42

    def test_full_brief(self):
        brief = ContextBrief(
            relevant_files=[FileContext(path="a.py", purpose="module A")],
            patterns=["async by default"],
            dependencies=["a.py imports b.py"],
            acceptance_criteria=["pytest passes"],
            risks=["no tests exist"],
            suggested_approach="Add tests first",
        )
        assert len(brief.relevant_files) == 1
        assert brief.patterns == ["async by default"]
        assert brief.suggested_approach == "Add tests first"

    def test_brief_from_json(self):
        data = {
            "relevant_files": [{"path": "x.py", "purpose": "test"}],
            "patterns": ["pattern1"],
            "dependencies": [],
            "acceptance_criteria": ["tests pass"],
            "risks": [],
            "suggested_approach": "do it",
        }
        brief = ContextBrief.model_validate(data)
        assert brief.relevant_files[0].path == "x.py"
        assert brief.acceptance_criteria == ["tests pass"]

    def test_brief_to_json_roundtrip(self):
        brief = ContextBrief(
            relevant_files=[FileContext(path="a.py", purpose="mod", line_count=10)],
            patterns=["p1"],
        )
        data = brief.model_dump()
        restored = ContextBrief.model_validate(data)
        assert restored == brief


class TestFormatBriefForPrompt:
    """Tests for formatting a ContextBrief into prompt text."""

    def test_empty_brief(self):
        brief = ContextBrief()
        result = format_brief_for_prompt(brief)
        assert result == "No codebase context available."

    def test_brief_with_files(self):
        brief = ContextBrief(
            relevant_files=[FileContext(path="src/main.py", purpose="Entry point")],
        )
        result = format_brief_for_prompt(brief)
        assert "src/main.py" in result
        assert "Entry point" in result
        assert "Relevant files:" in result

    def test_brief_with_all_fields(self):
        brief = ContextBrief(
            relevant_files=[FileContext(path="a.py", purpose="mod")],
            patterns=["async default"],
            dependencies=["a imports b"],
            acceptance_criteria=["tests pass"],
            risks=["no tests"],
            suggested_approach="careful approach",
        )
        result = format_brief_for_prompt(brief)
        assert "Relevant files:" in result
        assert "Codebase patterns:" in result
        assert "Dependencies:" in result
        assert "Acceptance criteria:" in result
        assert "Risks:" in result
        assert "Suggested approach:" in result

    def test_files_capped_at_15(self):
        files = [FileContext(path=f"f{i}.py", purpose=f"file {i}") for i in range(20)]
        brief = ContextBrief(relevant_files=files)
        result = format_brief_for_prompt(brief)
        # Should only include 15 files
        assert "f14.py" in result
        assert "f15.py" not in result


class TestExtractPatterns:
    """Tests for extracting convention patterns from CLAUDE.md content."""

    def test_extracts_bold_key_value_patterns(self):
        content = "- **Ruff**: py312, line-length 120\n- **Tests**: pytest + pytest-asyncio"
        patterns = _extract_patterns(content)
        assert len(patterns) == 2
        assert "Ruff: py312, line-length 120" in patterns

    def test_extracts_convention_keywords(self):
        content = "- Always use async for I/O\n- Some other note\n- Use Pydantic models for data"
        patterns = _extract_patterns(content)
        assert any("async" in p for p in patterns)
        assert any("Pydantic" in p for p in patterns)

    def test_caps_at_10(self):
        lines = [f"- **Key{i}**: value{i}" for i in range(20)]
        patterns = _extract_patterns("\n".join(lines))
        assert len(patterns) <= 10

    def test_empty_content(self):
        assert _extract_patterns("") == []


class TestInferPurpose:
    """Tests for inferring file purpose from docstrings/comments."""

    def test_infer_from_docstring(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        f.write_text('"""Task decomposition module."""\n\nimport os\n')
        assert _infer_purpose(f) == "Task decomposition module."

    def test_infer_from_comment(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        f.write_text("# Helper utilities\n\nimport os\n")
        assert _infer_purpose(f) == "Helper utilities"

    def test_no_docstring(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        f.write_text("import os\n\ndef foo(): pass\n")
        assert _infer_purpose(f) == ""

    def test_shebang_skipped(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        f.write_text("#!/usr/bin/env python\n# The real purpose\nimport os\n")
        assert _infer_purpose(f) == "The real purpose"

    def test_unreadable_file(self, tmp_path: Path):
        f = tmp_path / "binary.dat"
        f.write_bytes(b"\x00\x01\x02\x03")
        # Should not raise
        assert _infer_purpose(f) == ""


class TestScanSourceFiles:
    """Tests for scanning and ranking source files by task relevance."""

    def test_finds_python_files(self, tmp_path: Path):
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "planner.py").write_text('"""Planner module."""\n')
        (src / "dispatch.py").write_text('"""Dispatch module."""\n')

        files = _scan_source_files(tmp_path, "update planner")
        paths = [f.path for f in files]
        assert any("planner" in p for p in paths)

    def test_scores_relevant_files_higher(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "planner.py").write_text('"""Planner."""\n')
        (src / "unrelated.py").write_text('"""Unrelated."""\n')

        files = _scan_source_files(tmp_path, "fix planner bug")
        # planner.py should come first (higher relevance)
        assert files[0].path == "src/planner.py"

    def test_skips_pycache(self, tmp_path: Path):
        cache = tmp_path / "src" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.cpython-312.pyc").write_text("")
        (tmp_path / "src" / "mod.py").write_text('"""Module."""\n')

        files = _scan_source_files(tmp_path, "anything")
        paths = [f.path for f in files]
        assert not any("__pycache__" in p for p in paths)

    def test_caps_at_max_files(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(50):
            (src / f"mod{i}.py").write_text(f'"""Module {i}."""\n')

        files = _scan_source_files(tmp_path, "anything")
        assert len(files) <= 30


class TestProgrammaticScout:
    """Tests for the full programmatic scout function."""

    def test_returns_context_brief(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text("- **Ruff**: py312\n- **Tests**: pytest\n")
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text('"""Main entry point."""\n')

        brief = programmatic_scout("add a feature", repo_root=tmp_path)
        assert isinstance(brief, ContextBrief)
        assert len(brief.relevant_files) > 0
        assert len(brief.patterns) > 0
        assert "pytest passes" in brief.acceptance_criteria

    def test_works_without_claude_md(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text('"""A module."""\n')

        brief = programmatic_scout("do something", repo_root=tmp_path)
        assert isinstance(brief, ContextBrief)
        assert brief.patterns == []

    def test_works_with_empty_dir(self, tmp_path: Path):
        brief = programmatic_scout("anything", repo_root=tmp_path)
        assert isinstance(brief, ContextBrief)
        assert brief.relevant_files == []

    def test_on_real_repo(self):
        """Run against the actual horse-fish repo to verify it works end-to-end."""
        repo_root = Path(__file__).parent.parent
        brief = programmatic_scout("update the planner to use context briefs", repo_root=repo_root)
        assert isinstance(brief, ContextBrief)
        assert len(brief.relevant_files) > 0
        # Should find planner-related files
        paths = [f.path for f in brief.relevant_files]
        assert any("planner" in p for p in paths)


class TestScoutPromptTemplate:
    """Tests for the scout prompt template."""

    def test_template_has_placeholders(self):
        assert "{task}" in SCOUT_PROMPT_TEMPLATE
        assert "{project_context}" in SCOUT_PROMPT_TEMPLATE

    def test_template_formats_without_error(self):
        result = SCOUT_PROMPT_TEMPLATE.format(task="add a feature", project_context="Python project")
        assert "add a feature" in result
        assert "Python project" in result


class TestSmartPlannerWithBrief:
    """Tests for SmartPlanner integration with ContextBrief."""

    @pytest.fixture
    def mock_planner(self):
        """Create a mock Planner."""
        from unittest.mock import AsyncMock, MagicMock

        planner = MagicMock()
        planner.runtime = "pi"
        planner.model = "qwen3.5-plus"
        planner._tracer = None
        planner._build_command = MagicMock(return_value=["echo", "SOLO"])
        planner._run_cli = AsyncMock(return_value="SOLO")
        return planner

    @pytest.mark.asyncio
    async def test_decompose_with_brief(self, mock_planner):
        from horse_fish.planner.smart import SmartPlanner

        sp = SmartPlanner(mock_planner)
        brief = ContextBrief(
            relevant_files=[FileContext(path="src/main.py", purpose="entry")],
            acceptance_criteria=["tests pass", "lint clean"],
        )
        subtasks, complexity = await sp.decompose("add feature", context_brief=brief)
        assert len(subtasks) == 1
        # SOLO mode should propagate acceptance criteria
        assert subtasks[0].acceptance_criteria == ["tests pass", "lint clean"]

    @pytest.mark.asyncio
    async def test_decompose_without_brief(self, mock_planner):
        from horse_fish.planner.smart import SmartPlanner

        sp = SmartPlanner(mock_planner)
        subtasks, complexity = await sp.decompose("add feature")
        assert len(subtasks) == 1
        assert subtasks[0].acceptance_criteria == []

    @pytest.mark.asyncio
    async def test_classify_includes_codebase_context(self, mock_planner):
        from horse_fish.planner.smart import SmartPlanner

        sp = SmartPlanner(mock_planner)
        brief = ContextBrief(
            relevant_files=[FileContext(path="a.py", purpose="mod")],
            patterns=["async default"],
        )
        subtasks, complexity = await sp.decompose("task", context_brief=brief)
        # Verify the classify prompt was called with codebase context
        call_args = mock_planner._build_command.call_args
        prompt_text = call_args[0][0]
        assert "Codebase context:" in prompt_text


class TestParseScoutOutput:
    """Tests for parsing ContextBrief JSON from raw scout agent output."""

    def test_empty_output(self):
        assert parse_scout_output("") is None
        assert parse_scout_output(None) is None

    def test_valid_json_in_fences(self):
        raw = (
            "Let me explore the codebase.\n\n"
            "```json\n"
            '{"relevant_files": [{"path": "src/main.py", "purpose": "entry"}], '
            '"patterns": ["async"], "dependencies": [], '
            '"acceptance_criteria": ["tests pass"], "risks": [], '
            '"suggested_approach": "add it"}\n'
            "```\n"
        )
        brief = parse_scout_output(raw)
        assert brief is not None
        assert brief.relevant_files[0].path == "src/main.py"
        assert brief.patterns == ["async"]
        assert brief.acceptance_criteria == ["tests pass"]

    def test_valid_json_bare(self):
        raw = (
            "Here is the context:\n"
            '{"relevant_files": [], "patterns": ["ruff"], '
            '"dependencies": [], "acceptance_criteria": [], '
            '"risks": ["no tests"], "suggested_approach": ""}'
        )
        brief = parse_scout_output(raw)
        assert brief is not None
        assert brief.risks == ["no tests"]

    def test_invalid_json(self):
        assert parse_scout_output("not json at all") is None
        assert parse_scout_output("{broken json") is None

    def test_json_array_rejected(self):
        # A plain array with no object structure should fail
        assert parse_scout_output("[1, 2, 3]") is None

    def test_partial_fields_ok(self):
        """ContextBrief has defaults, so partial JSON should still parse."""
        raw = '{"relevant_files": [], "patterns": ["convention1"]}'
        brief = parse_scout_output(raw)
        assert brief is not None
        assert brief.patterns == ["convention1"]
        assert brief.risks == []

    def test_json_with_surrounding_noise(self):
        raw = (
            "I analyzed the project. Here are my findings:\n\n"
            '{"relevant_files": [{"path": "x.py", "purpose": "test"}], '
            '"suggested_approach": "simple change"}\n\n'
            "Hope this helps!"
        )
        brief = parse_scout_output(raw)
        assert brief is not None
        assert brief.relevant_files[0].path == "x.py"

    def test_nested_json_takes_outermost(self):
        raw = '{"relevant_files": [], "suggested_approach": "use {braces} carefully"}'
        brief = parse_scout_output(raw)
        assert brief is not None
