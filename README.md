# War.gov UFO Release Markdown Archive

This repository is the public archive for Markdown files converted from the official UFO/UAP release at [war.gov/UFO](https://www.war.gov/UFO/).

The source page is the Department of War's "Presidential Unsealing and Reporting System for UAP Encounters (PURSUE)" page. It describes a government-wide effort, supported by ODNI, to identify, review, declassify, and release unresolved UAP-related records and historical documents. Release 01 is marked "Cleared for release - May 8, 2026."

The primary content of this repo is the `converted/` tree. Each converted source file gets its own folder, and each source page gets its own Markdown file.

## Archive Structure

```text
converted/
├── 001-65_HS1-834228961_62-HQ-83894_Section_10/
│   ├── page-0001.md
│   ├── page-0002.md
│   └── ...
├── 002-65_HS1-834228961_62-HQ-83894_Section_2/
│   ├── page-0001.md
│   ├── page-0002.md
│   └── ...
└── manifest.jsonl
```

Page files use zero-padded page numbers so lexical order matches page order.

Each `page-####.md` file includes YAML front matter:

```yaml
---
source_title: "..."
source_file: "..."
source_url: "..."
asset_type: "pdf"
dataset_row: 1
page: 1
page_count: 184
model: "gemini-3.1-flash-lite"
generated_at: "..."
---
```

`converted/manifest.jsonl` records one JSON line per page attempt, including the source file, page number, output path, model output character count, status, and error details if a page failed.

## What Is Tracked

```text
.
├── converted/
│   └── .gitkeep
├── metadata/
│   └── uap-csv.csv
├── scripts/
│   └── process_dataset_with_gemini.py
├── requirements.txt
└── README.md
```

- `converted/` is the destination for committed Markdown transcripts.
- `metadata/uap-csv.csv` is the release inventory used to map source records to converted folders. At repo preparation time it contained 162 rows: 120 PDF rows, 28 video rows, and 14 image rows.
- `scripts/process_dataset_with_gemini.py` is the support script used to produce the Markdown archive.
- `requirements.txt` lists the script dependencies.

Local-only folders are ignored:

- `downloads/` stores source PDFs/images fetched from war.gov.
- `outputs/` stores temporary smoke-test outputs.
- `source/` stores local page snapshots used during scraping/debugging.
- `node_modules/`, `.venv/`, `.env`, caches, and `.DS_Store` are local-only.

## How The Files Were Converted

The conversion was done page by page:

1. Read the release inventory from `metadata/uap-csv.csv`.
2. Download or match each supported PDF/image source into `downloads/war-gov-ufo-release-1/`.
3. Render every PDF page with PyMuPDF at 200 DPI.
4. Resize the longest side to at most 3000 pixels and encode the rendered page as JPEG.
5. Send the page image and a transcription prompt to Gemini.
6. Save Gemini's returned Markdown as `converted/<source-folder>/page-####.md`.
7. Append the page result to `converted/manifest.jsonl`.

The default model used by the support script is `gemini-3.1-flash-lite`. The script supports parallel page workers and a global request-per-minute gate.

Generated Markdown should be treated as AI-assisted OCR/transcription. Use the original source PDF/image as the authoritative record when exact wording matters.

## Rebuilding Or Continuing The Archive

Use Python 3.11+.

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Set the Gemini key in the shell. Do not commit API keys.

```sh
export GEMINI_API_KEY='...'
```

Dry-run the dataset without calling Gemini:

```sh
python3 scripts/process_dataset_with_gemini.py --dry-run
```

Generate or continue the committed archive:

```sh
python3 scripts/process_dataset_with_gemini.py \
  --download-missing \
  --output-dir converted \
  --workers 16 \
  --rpm 10000
```

The script is resumable. Existing `page-####.md` files are skipped unless `--force` is passed.

Useful options:

- `--workers N` controls page-level parallelism. It can also be set with `GEMINI_WORKERS`.
- `--rpm N` controls the global Gemini request-per-minute gate. It can also be set with `GEMINI_RPM`.
- `--pages 1,4,9-12` processes selected pages only.
- `--max-docs N` and `--max-pages-per-doc N` are useful for smoke tests.
- `--local-only` ignores metadata downloads and processes files already present in `downloads/`.
- `--force` regenerates pages that already have Markdown outputs.
- `--stop-on-error` stops the run after the first page error.

## Notes For Public Use

- The source materials are public government records from the official war.gov release page.
- The repository is intended to hold the converted Markdown archive, not the 2+ GB downloaded source corpus.
- Keep `.env`, API keys, downloaded records, local page snapshots, smoke-test outputs, and caches out of git.
