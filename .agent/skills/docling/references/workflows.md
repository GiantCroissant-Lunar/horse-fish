# Docling Workflows

Use these examples only when the main skill needs concrete command or code shapes.

## Quick CLI Conversion

Convert a single file or URL to Markdown:

```bash
uvx --from docling docling myfile.pdf
uvx --from docling docling https://arxiv.org/pdf/2206.01062
```

Convert to multiple output formats:

```bash
uvx --from docling docling myfile.pdf --to md --to json
```

Convert a directory and write outputs to a folder:

```bash
uvx --from docling docling ./input --from pdf --from docx --to md --to json --output ./scratch
```

Abort on first batch error:

```bash
uvx --from docling docling ./input --output ./scratch --abort-on-error
```

## Offline / Prefetched Models

Prefetch models:

```bash
uvx --from docling docling-tools models download
```

Use prefetched artifacts:

```bash
uvx --from docling docling --artifacts-path="/local/path/to/models" myfile.pdf
```

## Python API

Basic conversion to Markdown:

```bash
uv run --with docling python - <<'PY'
from docling.document_converter import DocumentConverter

converter = DocumentConverter()
result = converter.convert("myfile.pdf")
print(result.document.export_to_markdown())
PY
```

Limit document size in code:

```bash
uv run --with docling python - <<'PY'
from docling.document_converter import DocumentConverter

converter = DocumentConverter()
result = converter.convert("myfile.pdf", max_num_pages=100, max_file_size=20971520)
print(result.document.export_to_markdown())
PY
```

## OCR / Enrichments / VLM

Run VLM pipeline from CLI:

```bash
uvx --from docling docling --pipeline vlm myfile.pdf
```

Enable enrichments from CLI:

```bash
uvx --from docling docling --enrich-code myfile.pdf
uvx --from docling docling --enrich-formula myfile.pdf
uvx --from docling docling --enrich-picture-classes myfile.pdf
```

Use Python for custom PDF pipeline options:

```bash
uv run --with docling python - <<'PY'
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

pipeline_options = PdfPipelineOptions()
pipeline_options.do_ocr = True
pipeline_options.do_table_structure = True

converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
    }
)

result = converter.convert("myfile.pdf")
print(result.document.export_to_markdown())
PY
```

## Remote Service Opt-In

Docling is local-first. If a task needs remote inference services, mention the privacy/runtime tradeoff and use code that explicitly enables remote services:

```python
pipeline_options.enable_remote_services = True
```
