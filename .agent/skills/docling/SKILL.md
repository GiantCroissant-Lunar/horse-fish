---
name: docling
description: "Use when the user wants structured document conversion or extraction with Docling: converting PDFs, DOCX, PPTX, HTML, images, audio, or similar inputs into Markdown, JSON, HTML, plain text, DocTags, or structured document representations. Prefer the Docling CLI for one-off conversions and the Python API for custom pipelines, OCR, enrichments, VLM workflows, or batch processing."
---

# Docling

Use this skill for document parsing and conversion tasks. Prefer it when the user needs faithful document structure, tables, OCR, multimodal extraction, or machine-readable output from files or document URLs.

Do not use this skill for ordinary web research or generic text summarization when no document conversion is needed.

## Tool Choice

1. Prefer the CLI for straightforward conversions and batch jobs.
2. Prefer the Python API when the user needs custom pipeline options, per-format settings, chunking, enrichments, or integration into code.
3. Do not assume Docling is installed globally. Safe ad hoc commands in this project are:

```bash
uvx --from docling docling ...
uv run --with docling python ...
```

## Default Workflow

1. Identify the source type: local file, directory, URL, PDF/image scan, office document, HTML, or audio.
2. Choose the lightest useful path:
   - one file or directory conversion -> CLI
   - custom OCR, VLM, enrichments, or code integration -> Python API
3. Default to Markdown for human-readable output.
4. Use JSON or Docling document exports when downstream code needs structure.
5. If the task is large, scanned, or multimodal, warn that models may download on first use and conversion may take time.

## Practical Rules

1. Prefer local processing by default.
2. If the user requests remote model APIs or cloud-backed vision services, note that Docling requires explicit opt-in for remote services in code.
3. For scanned PDFs or images, expect OCR-related model usage.
4. For complex visual extraction, VLM and enrichment paths are slower and heavier than standard conversion.
5. If the user needs offline or repeatable runs, suggest prefetching models with `docling-tools models download`.

## Common Intent Mapping

- "convert this PDF to markdown" -> CLI conversion to Markdown
- "extract structured text and tables from these files" -> CLI batch conversion or Python API if customization is needed
- "parse this scanned PDF" -> OCR-aware Docling workflow
- "use code to process these documents" -> Python `DocumentConverter`
- "run a VLM or image-aware pipeline" -> Docling VLM pipeline
- "caption figures / enrich formulas / classify pictures" -> Docling enrichment pipeline

## Fallback Documentation

If syntax is unclear, consult [references/workflows.md](references/workflows.md) first.

If more detail is needed:

```bash
uvx --from docling docling --help
uvx --from docling docling-tools --help
```

## Pause Conditions

Pause and confirm with the user when:

- the expected output format is unclear
- the user may not want heavy OCR/VLM processing time
- remote services would be required or materially change privacy characteristics
- the user has not chosen between a quick conversion and a custom code workflow
