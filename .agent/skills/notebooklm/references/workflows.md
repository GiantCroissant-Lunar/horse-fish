# NotebookLM Workflows

Use these examples only when the main skill needs concrete command shapes.

## Auth Check

```bash
uvx --from notebooklm-mcp-cli nlm login --check
uvx --from notebooklm-mcp-cli nlm login
```

## Create Notebook and Add Sources

```bash
uvx --from notebooklm-mcp-cli nlm notebook create "Research Notebook"
uvx --from notebooklm-mcp-cli nlm source add <notebook-id> --url "https://example.com"
uvx --from notebooklm-mcp-cli nlm source add <notebook-id> --text "Notes..." --title "Working Notes"
uvx --from notebooklm-mcp-cli nlm source add <notebook-id> --drive <doc-id>
```

## Query Notebook

```bash
uvx --from notebooklm-mcp-cli nlm notebook query <notebook-id> "What are the main themes?"
uvx --from notebooklm-mcp-cli nlm notebook query <notebook-id> "Expand on the first theme" --conversation-id <conversation-id>
```

## Research Pipeline

```bash
uvx --from notebooklm-mcp-cli nlm research start "topic" --notebook-id <notebook-id>
uvx --from notebooklm-mcp-cli nlm research status <notebook-id>
uvx --from notebooklm-mcp-cli nlm research import <notebook-id> <task-id>
```

## Artifact Generation

```bash
uvx --from notebooklm-mcp-cli nlm audio create <notebook-id> --confirm
uvx --from notebooklm-mcp-cli nlm report create <notebook-id> --confirm
uvx --from notebooklm-mcp-cli nlm slides create <notebook-id> --confirm
uvx --from notebooklm-mcp-cli nlm studio status <notebook-id>
```

## Deletion Safety

Never run delete commands without explicit user confirmation:

```bash
uvx --from notebooklm-mcp-cli nlm source delete <source-id> --confirm
uvx --from notebooklm-mcp-cli nlm notebook delete <notebook-id> --confirm
```
