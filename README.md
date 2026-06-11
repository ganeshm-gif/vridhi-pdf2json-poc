# PDF Valuation Report Extraction Pipeline

## Overview

`dataextract.py` is a production‑grade command‑line tool that extracts structured data from valuation report PDFs. It parses text blocks with **PyMuPDF**, extracts tables with **pdfplumber**, builds an intermediate JSON representation, converts it to markdown, and then normalises it to a clean JSON schema using the OpenRouter LLM (Gemini 2.5 Flash). The confidence‑check step has been removed for a leaner workflow.

## Installation

```bash
# Create a virtual environment (optional but recommended)
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Environment Variables

Create a `.env` file in the project root with your OpenRouter API key:

```
OPENROUTER_API_KEY=your_api_key_here
```

## Usage

```bash
python g.py path/to/your/document.pdf \
    --output output.json \
    --raw-output raw_extracted.json
```

- `--output` – final normalized JSON file (default: `output.json`).
- `--raw-output` – intermediate extraction result (default: `raw_extracted.json`).

## What It Does
1. **Extract text blocks** (bold detection) with PyMuPDF.
2. **Extract tables** with pdfplumber.
3. **Combine** the data into an intermediate JSON structure.
4. **Render** the structure to markdown.
5. **Send** the markdown to OpenRouter for LLM normalisation.
6. **Validate** the LLM output with a Pydantic model.
7. **Save** the final JSON.

## License

MIT License – see `LICENSE` file for details.
