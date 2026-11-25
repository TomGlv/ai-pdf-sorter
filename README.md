# ai-pdf-sorter

> Sorts handwritten scanned PDFs with an LLM vision model (Claude): structured extraction, human-review queue, cache, CSV audit.

## What it does
Reads **handwritten scanned PDFs** (competition score sheets) with a multimodal LLM, extracts the **year** and the **team format**
(tête-à-tête, doublette, triplette…) as structured data, and files every document automatically:

```
<output>/<year>/<type>/<file>.pdf
<output>/<year>/À vérifier/<file>.pdf     # uncertain → human review
<output>/Année inconnue/<type>/<file>.pdf
```

## Why an LLM and not Tesseract
I benchmarked classic OCR (Tesseract) on the handwriting first: unusable. A vision LLM reads it reliably, and returns a
**Pydantic-validated JSON** answer with a confidence flag.

## Reliability features
- **"To verify" queue**: anything uncertain is routed to a human instead of being misfiled
- **Cache** (`.cache_lecture.json`, keyed by file hash): re-running never pays inference twice
- **Dry-run** mode (`--essai`) and move mode (`--deplacer`)
- **CSV audit report** (`recensement.csv`) listing what was read for every file

## Run
```bash
pip install -r requirements.txt
set ANTHROPIC_API_KEY=...
python trier_pdfs.py "C:\scans" "C:\sorted"
```

---
Made by [Tom Galvan](https://tomglv.dev) · Bachelor in AI, ECE Paris
