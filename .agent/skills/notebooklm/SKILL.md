---
name: notebooklm
description: "Use when the user explicitly wants to work with Google NotebookLM: create or manage notebooks, add sources, run NotebookLM research, ask questions over sources, or generate NotebookLM artifacts such as podcasts, reports, quizzes, flashcards, mind maps, slides, infographics, videos, or data tables. Prefer the project MCP server named `notebooklm`. If the MCP server is unavailable, fall back to the `notebooklm-mcp-cli` CLI via `uvx`."
---

# NotebookLM

Use this skill only for explicit NotebookLM work. Do not use it for ordinary web research, local file summarization, or generic note-taking unless the user asks for NotebookLM specifically.

## Tool Choice

1. Prefer the project MCP server `notebooklm` when it is available.
2. If MCP is unavailable, use the CLI via:

```bash
uvx --from notebooklm-mcp-cli nlm ...
```

3. Do not assume `nlm` is installed globally. The `uvx --from ...` form is the safe fallback in this project.

## Authentication

Before the first NotebookLM action in a session, verify auth:

```bash
uvx --from notebooklm-mcp-cli nlm login --check
```

If auth is missing or expired, stop and ask the user to run:

```bash
uvx --from notebooklm-mcp-cli nlm login
```

NotebookLM sessions expire. Re-check auth when commands start failing.

## Safety Rules

1. Ask for explicit confirmation before any delete operation.
2. Never use `nlm chat start`; it opens an interactive REPL. Use notebook query operations instead.
3. Capture notebook IDs, task IDs, conversation IDs, and artifact IDs from outputs because later steps depend on them.
4. Use compact/default output unless structured parsing is required. Use JSON only when needed.
5. Treat NotebookLM as the source-specific workspace. Keep normal web search outside this skill unless the user explicitly wants sources imported into NotebookLM.

## Default Operating Pattern

1. Check whether MCP `notebooklm` is available.
2. Verify auth.
3. Use the shortest path that matches the user request:
   - create notebook
   - add or inspect sources
   - query notebook
   - start research, poll status, import sources
   - generate artifact, then check studio status
4. If details are unclear, consult [references/workflows.md](references/workflows.md).
5. If command syntax is still unclear in CLI mode, use:

```bash
uvx --from notebooklm-mcp-cli nlm --ai
uvx --from notebooklm-mcp-cli nlm <command> --help
```

## Common Intent Mapping

- "make a NotebookLM notebook for these links" -> create notebook, add sources
- "research this topic in NotebookLM" -> create or identify notebook, start research, poll, import
- "ask NotebookLM about these sources" -> notebook query
- "make a NotebookLM podcast/report/slides" -> generate artifact, then check status

## Refusal / Pause Conditions

Pause and ask the user instead of proceeding when:

- authentication is missing
- the request is destructive
- the notebook or source target is ambiguous
- both NotebookLM and a non-NotebookLM path are plausible and the user has not clearly chosen NotebookLM
