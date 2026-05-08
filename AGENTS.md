# Repository Instructions

- This is the public repository for converted Markdown files. The primary tracked artifact tree is `converted/`.
- Commit docs, `metadata/uap-csv.csv`, support tooling, and converted Markdown. Do not commit `downloads/`, `outputs/`, `source/`, `node_modules/`, `.env`, API keys, or caches.
- Use `scripts/process_dataset_with_gemini.py --output-dir converted` for conversion work. It writes one folder per source file and one Markdown file per processed page.
- Before changing the converter, run `python3 -m py_compile scripts/process_dataset_with_gemini.py`.
- For live smoke tests, use a small page set with `--pages`, `--max-docs`, or `--max-pages-per-doc`.
- Treat generated Markdown as AI-assisted OCR. The source PDF/image remains authoritative.
- Keep dataset cleanup deterministic. Use explicit, reviewable rules for archive-wide edits such as scrubbing local absolute paths, removing API-key leaks, normalizing manifest paths, or collapsing obvious repeated-token artifacts. Verify counts before and after cleanup.
- Do not expose local usernames, home-directory paths, Dropbox paths, machine-specific absolute paths, or other private workstation details in README text, Markdown front matter, manifests, logs, or committed examples. Public paths should be repo-relative.
- Do not commit helper cleanup scripts unless the user explicitly asks for them. Temporary one-off cleanup/report scripts can be run from the shell or local ignored paths, then removed.
- If a page cannot be converted by Gemini, do not leave a permanent `.error.txt` placeholder when the user wants a complete archive. Render the source page locally, inspect it, and use deterministic local OCR/text extraction first.
- For difficult pages, use manual image-to-text with Codex from the rendered page image. It is acceptable to write a concise manual page description when a page is image-only, extremely faint, or not reliably transcribable.
- After any dataset repair, verify `converted/` has the expected number of `page-*.md` files, no retained `.error.txt` files unless intentionally documented, and `converted/manifest.jsonl` has one final `ok` row per converted page with no duplicate `(asset, page)` keys.
