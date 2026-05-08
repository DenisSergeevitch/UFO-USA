# Repository Instructions

- This is the public repository for converted Markdown files. The primary tracked artifact tree is `converted/`.
- Commit docs, `metadata/uap-csv.csv`, support tooling, and converted Markdown. Do not commit `downloads/`, `outputs/`, `source/`, `node_modules/`, `.env`, API keys, or caches.
- Use `scripts/process_dataset_with_gemini.py --output-dir converted` for conversion work. It writes one folder per source file and one Markdown file per processed page.
- Before changing the converter, run `python3 -m py_compile scripts/process_dataset_with_gemini.py`.
- For live smoke tests, use a small page set with `--pages`, `--max-docs`, or `--max-pages-per-doc`.
- Treat generated Markdown as AI-assisted OCR. The source PDF/image remains authoritative.
