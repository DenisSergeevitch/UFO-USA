#!/usr/bin/env python3
"""Convert the UFO release dataset into page-level Markdown with Gemini."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_WORKERS = max(1, min(16, (os.cpu_count() or 4) * 2))
DEFAULT_RPM = 10_000

DEFAULT_PROMPT = """Transcribe and normalize this scanned government UFO/UAP source page into Markdown.

Rules:
- Preserve the original content as much as possible, including headings, dates, places, names, stamps, handwritten notes, tables, letterheads, marginalia, redactions, and page numbers.
- Do not invent missing text. Use [illegible] for unreadable text and [redacted] for blacked-out content.
- Keep line breaks and table structure where they help readability.
- If the page is blank, mostly blank, or only contains an image, state that concisely.
- Return only Markdown for this page. Do not wrap the answer in code fences.
"""


@dataclass(frozen=True)
class Asset:
    title: str
    asset_type: str
    source_url: str
    local_path: Path
    row_number: int | None = None
    release_date: str = ""
    agency: str = ""
    description: str = ""

    @property
    def slug(self) -> str:
        prefix = f"{self.row_number:03d}-" if self.row_number is not None else ""
        return prefix + slugify(self.title or self.local_path.stem)


@dataclass(frozen=True)
class PageTask:
    asset: Asset
    page_number: int
    page_count: int
    output_path: Path


class RequestRateLimiter:
    def __init__(self, rpm: int) -> None:
        self.rpm = rpm
        self.timestamps: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        if self.rpm <= 0:
            return

        while True:
            with self.lock:
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= 60:
                    self.timestamps.popleft()

                if len(self.timestamps) < self.rpm:
                    self.timestamps.append(now)
                    return

                sleep_for = 60 - (now - self.timestamps[0])
            time.sleep(max(0.05, sleep_for))


def slugify(value: str, max_len: int = 120) -> str:
    value = value.strip().replace("\n", " ")
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("._-")
    return (value or "untitled")[:max_len]


def normalized_name(value: str) -> str:
    value = urllib.parse.unquote(value).lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def remote_filename(url: str) -> str | None:
    try:
        path = urllib.parse.urlsplit(url.strip()).path
    except ValueError:
        return None
    name = urllib.parse.unquote(Path(path).name)
    return name or None


def remote_extension(url: str) -> str:
    name = remote_filename(url) or ""
    return Path(name).suffix.lower()


def quote_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%:@")
    query = urllib.parse.quote(urllib.parse.unquote_plus(parts.query), safe="=&%:@+,-._~")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def yaml_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def build_local_index(downloads_dir: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    if not downloads_dir.exists():
        return index
    for path in downloads_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            index.setdefault(normalized_name(path.name), path)
    return index


def download_asset(url: str, downloads_dir: Path, fallback_name: str) -> Path:
    name = remote_filename(url) or fallback_name
    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported or missing asset extension in URL: {url}")

    output_path = downloads_dir / name
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        return output_path

    request = urllib.request.Request(
        quote_url(url),
        headers={"User-Agent": "Mozilla/5.0 (compatible; UFO-USA dataset processor)"},
    )
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            tmp_path.write_bytes(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise RuntimeError(f"Download failed for {url}: {exc}") from exc

    tmp_path.replace(output_path)
    return output_path


def read_metadata_assets(
    metadata_path: Path,
    downloads_dir: Path,
    *,
    download_missing: bool,
) -> tuple[list[Asset], list[dict[str, str]]]:
    assets: list[Asset] = []
    skipped: list[dict[str, str]] = []
    local_index = build_local_index(downloads_dir)

    with metadata_path.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=1):
            title = (row.get("Title") or "").strip()
            url = (row.get("PDF | Image Link") or "").strip()
            ext = remote_extension(url)
            if not url or ext not in SUPPORTED_EXTENSIONS:
                skipped.append({"row": str(row_number), "title": title, "reason": "no supported asset URL"})
                continue

            local_path = local_index.get(normalized_name(remote_filename(url) or ""))
            if local_path is None and download_missing:
                try:
                    local_path = download_asset(url, downloads_dir, slugify(title) + ext)
                    local_index[normalized_name(local_path.name)] = local_path
                except Exception as exc:  # noqa: BLE001 - keep batch processing resilient.
                    skipped.append({"row": str(row_number), "title": title, "reason": str(exc)})
                    continue

            if local_path is None:
                skipped.append({"row": str(row_number), "title": title, "reason": "asset not downloaded"})
                continue

            assets.append(
                Asset(
                    title=title or local_path.stem,
                    asset_type=(row.get("Type") or ext.lstrip(".")).strip(),
                    source_url=url,
                    local_path=local_path,
                    row_number=row_number,
                    release_date=(row.get("Release Date") or "").strip(),
                    agency=(row.get("Agency") or "").strip(),
                    description=(row.get("Description Blurb") or "").strip(),
                )
            )

    return assets, skipped


def read_local_assets(downloads_dir: Path) -> list[Asset]:
    assets: list[Asset] = []
    for path in sorted(downloads_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            assets.append(
                Asset(
                    title=path.stem,
                    asset_type=path.suffix.lower().lstrip("."),
                    source_url="",
                    local_path=path,
                )
            )
    return assets


def read_pdf_manifest_assets(
    pdf_manifest_path: Path,
    downloads_dir: Path,
    *,
    download_missing: bool,
) -> tuple[list[Asset], list[dict[str, str]]]:
    assets: list[Asset] = []
    skipped: list[dict[str, str]] = []
    local_index = build_local_index(downloads_dir)

    with pdf_manifest_path.open(encoding="utf-8") as handle:
        for row_number, line in enumerate(handle, start=1):
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                skipped.append({"row": str(row_number), "title": "", "reason": "malformed PDF manifest row"})
                continue

            filename, url, title, agency, release_date, incident_date, incident_location = parts[:7]
            local_path = downloads_dir / filename
            if not local_path.exists():
                local_path = local_index.get(normalized_name(filename))

            if local_path is None and download_missing:
                try:
                    local_path = download_asset(url, downloads_dir, filename)
                    local_index[normalized_name(local_path.name)] = local_path
                except Exception as exc:  # noqa: BLE001 - keep batch processing resilient.
                    skipped.append({"row": str(row_number), "title": title, "reason": str(exc)})
                    continue

            if local_path is None:
                skipped.append({"row": str(row_number), "title": title, "reason": "PDF not downloaded"})
                continue
            if local_path.suffix.lower() != ".pdf":
                skipped.append({"row": str(row_number), "title": title, "reason": "manifest file is not a PDF"})
                continue

            details = []
            if incident_date:
                details.append(f"Incident date: {incident_date}")
            if incident_location:
                details.append(f"Incident location: {incident_location}")

            assets.append(
                Asset(
                    title=title or local_path.stem,
                    asset_type="PDF",
                    source_url=url,
                    local_path=local_path,
                    row_number=row_number,
                    release_date=release_date,
                    agency=agency,
                    description="; ".join(details),
                )
            )

    return assets, skipped


def parse_page_selection(value: str) -> set[int]:
    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start <= 0 or end < start:
                raise ValueError(f"Invalid page range: {part}")
            selected.update(range(start, end + 1))
        else:
            page = int(part)
            if page <= 0:
                raise ValueError(f"Invalid page number: {part}")
            selected.add(page)
    return selected


def selected_pages(total_pages: int, pages_arg: str, max_pages_per_doc: int) -> list[int]:
    if pages_arg:
        pages = sorted(page for page in parse_page_selection(pages_arg) if page <= total_pages)
    else:
        pages = list(range(1, total_pages + 1))
    if max_pages_per_doc:
        pages = pages[:max_pages_per_doc]
    return pages


def render_pdf_page(page, *, dpi: int, max_side: int, jpeg_quality: int) -> bytes:
    import fitz
    from PIL import Image

    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False, colorspace=fitz.csRGB)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    if max_side and max(image.size) > max_side:
        image.thumbnail((max_side, max_side))

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=jpeg_quality, optimize=True)
    return buffer.getvalue()


def load_image_asset(path: Path, *, max_side: int, jpeg_quality: int) -> tuple[bytes, str]:
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        if max_side and max(image.size) > max_side:
            image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=jpeg_quality, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def build_prompt(asset: Asset, page_number: int, page_count: int) -> str:
    context = [
        DEFAULT_PROMPT.strip(),
        "",
        f"Document title: {asset.title}",
        f"Source file: {asset.local_path.name}",
        f"Page: {page_number} of {page_count}",
    ]
    if asset.agency:
        context.append(f"Agency: {asset.agency}")
    if asset.release_date:
        context.append(f"Release date: {asset.release_date}")
    if asset.description:
        context.append(f"Dataset description: {asset.description}")
    return "\n".join(context)


def chunk_text_from_parts(chunk) -> str:
    texts: list[str] = []
    candidates = getattr(chunk, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                texts.append(text)
    if candidates:
        return "".join(texts)
    return getattr(chunk, "text", "") or ""


def gemini_page_markdown(
    client,
    types_module,
    *,
    model: str,
    thinking_level: str,
    prompt: str,
    image_bytes: bytes,
    mime_type: str,
    temperature: float,
    retries: int,
    retry_sleep: float,
    rate_limiter: RequestRateLimiter | None,
) -> str:
    config_kwargs: dict[str, object] = {"temperature": temperature}
    if thinking_level.lower() != "none":
        thinking_fields = getattr(types_module.ThinkingConfig, "model_fields", {})
        if "thinking_level" in thinking_fields:
            config_kwargs["thinking_config"] = types_module.ThinkingConfig(thinking_level=thinking_level)
        elif "thinking_budget" in thinking_fields:
            budget_by_level = {
                "LOW": 1024,
                "MEDIUM": 4096,
                "HIGH": 8192,
            }
            config_kwargs["thinking_config"] = types_module.ThinkingConfig(
                thinking_budget=budget_by_level.get(thinking_level.upper(), 8192)
            )
    config = types_module.GenerateContentConfig(**config_kwargs)

    contents = [
        types_module.Content(
            role="user",
            parts=[
                types_module.Part.from_text(text=prompt),
                types_module.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
        )
    ]

    for attempt in range(retries + 1):
        try:
            if rate_limiter is not None:
                rate_limiter.wait()
            chunks: list[str] = []
            for chunk in client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            ):
                if text := chunk_text_from_parts(chunk):
                    chunks.append(text)
            markdown = "".join(chunks).strip()
            if not markdown:
                raise RuntimeError("Gemini returned an empty response")
            return markdown
        except Exception:  # noqa: BLE001 - retry then surface the final failure.
            if attempt >= retries:
                raise
            time.sleep(retry_sleep * (2**attempt))

    raise RuntimeError("Gemini request failed unexpectedly")


def page_output_path(output_dir: Path, asset: Asset, page_number: int) -> Path:
    return output_dir / asset.slug / f"page-{page_number:04d}.md"


def build_markdown_file(
    asset: Asset,
    *,
    page_number: int,
    page_count: int,
    model: str,
    body: str,
) -> str:
    front_matter = {
        "source_title": asset.title,
        "source_file": str(asset.local_path),
        "source_url": asset.source_url,
        "asset_type": asset.asset_type,
        "dataset_row": asset.row_number,
        "page": page_number,
        "page_count": page_count,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines = ["---"]
    lines.extend(f"{key}: {yaml_value(value)}" for key, value in front_matter.items())
    lines.extend(
        [
            "---",
            "",
            f"# {asset.title} - Page {page_number}",
            "",
            body.strip(),
            "",
        ]
    )
    return "\n".join(lines)


def process_asset(
    asset: Asset,
    args: argparse.Namespace,
    client,
    types_module,
    rate_limiter: RequestRateLimiter | None = None,
) -> None:
    manifest_path = args.output_dir / "manifest.jsonl"
    suffix = asset.local_path.suffix.lower()

    if suffix == ".pdf":
        import fitz

        doc = fitz.open(asset.local_path)
        page_count = doc.page_count
        page_numbers = selected_pages(page_count, args.pages, args.max_pages_per_doc)

        print(f"{asset.slug}: {len(page_numbers)}/{page_count} pages")
        for page_number in page_numbers:
            output_path = page_output_path(args.output_dir, asset, page_number)
            if output_path.exists() and not args.force:
                print(f"  skip page {page_number}: exists")
                continue

            try:
                page = doc.load_page(page_number - 1)
                image_bytes = render_pdf_page(
                    page,
                    dpi=args.dpi,
                    max_side=args.max_side,
                    jpeg_quality=args.jpeg_quality,
                )
                prompt = build_prompt(asset, page_number, page_count)
                body = gemini_page_markdown(
                    client,
                    types_module,
                    model=args.model,
                    thinking_level=args.thinking_level,
                    prompt=prompt,
                    image_bytes=image_bytes,
                    mime_type="image/jpeg",
                    temperature=args.temperature,
                    retries=args.retries,
                    retry_sleep=args.retry_sleep,
                    rate_limiter=rate_limiter,
                )
                atomic_write_text(
                    output_path,
                    build_markdown_file(
                        asset,
                        page_number=page_number,
                        page_count=page_count,
                        model=args.model,
                        body=body,
                    ),
                )
                append_jsonl(
                    manifest_path,
                    {
                        "status": "ok",
                        "asset": asset.slug,
                        "source_file": str(asset.local_path),
                        "page": page_number,
                        "page_count": page_count,
                        "output": str(output_path),
                        "chars": len(body),
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                print(f"  wrote page {page_number}: {output_path}")
                if args.sleep:
                    time.sleep(args.sleep)
            except Exception as exc:  # noqa: BLE001 - continue long batch runs by default.
                error_path = output_path.with_suffix(".error.txt")
                atomic_write_text(error_path, f"{type(exc).__name__}: {exc}\n")
                append_jsonl(
                    manifest_path,
                    {
                        "status": "error",
                        "asset": asset.slug,
                        "source_file": str(asset.local_path),
                        "page": page_number,
                        "page_count": page_count,
                        "error": f"{type(exc).__name__}: {exc}",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                print(f"  error page {page_number}: {exc}", file=sys.stderr)
                if args.stop_on_error:
                    raise
    else:
        output_path = page_output_path(args.output_dir, asset, 1)
        if output_path.exists() and not args.force:
            print(f"{asset.slug}: skip image, exists")
            return

        image_bytes, mime_type = load_image_asset(
            asset.local_path,
            max_side=args.max_side,
            jpeg_quality=args.jpeg_quality,
        )
        prompt = build_prompt(asset, 1, 1)
        body = gemini_page_markdown(
            client,
            types_module,
            model=args.model,
            thinking_level=args.thinking_level,
            prompt=prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
            temperature=args.temperature,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
            rate_limiter=rate_limiter,
        )
        atomic_write_text(
            output_path,
            build_markdown_file(asset, page_number=1, page_count=1, model=args.model, body=body),
        )
        append_jsonl(
            manifest_path,
            {
                "status": "ok",
                "asset": asset.slug,
                "source_file": str(asset.local_path),
                "page": 1,
                "page_count": 1,
                "output": str(output_path),
                "chars": len(body),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print(f"{asset.slug}: wrote image page: {output_path}")


def build_page_tasks(assets: Iterable[Asset], args: argparse.Namespace) -> list[PageTask]:
    tasks: list[PageTask] = []
    skipped_existing = 0

    for asset in assets:
        suffix = asset.local_path.suffix.lower()
        if suffix == ".pdf":
            import fitz

            with fitz.open(asset.local_path) as doc:
                page_count = doc.page_count
            page_numbers = selected_pages(page_count, args.pages, args.max_pages_per_doc)
            print(f"{asset.slug}: {len(page_numbers)}/{page_count} pages")
        else:
            page_count = 1
            page_numbers = selected_pages(page_count, args.pages, args.max_pages_per_doc)
            print(f"{asset.slug}: {len(page_numbers)}/1 image page")

        for page_number in page_numbers:
            output_path = page_output_path(args.output_dir, asset, page_number)
            if output_path.exists() and not args.force:
                skipped_existing += 1
                continue
            tasks.append(
                PageTask(
                    asset=asset,
                    page_number=page_number,
                    page_count=page_count,
                    output_path=output_path,
                )
            )

    if skipped_existing:
        print(f"Skipping {skipped_existing} existing page output(s). Use --force to regenerate.")
    return tasks


def render_page_task(task: PageTask, args: argparse.Namespace) -> tuple[bytes, str]:
    if task.asset.local_path.suffix.lower() == ".pdf":
        import fitz

        with fitz.open(task.asset.local_path) as doc:
            page = doc.load_page(task.page_number - 1)
            return (
                render_pdf_page(
                    page,
                    dpi=args.dpi,
                    max_side=args.max_side,
                    jpeg_quality=args.jpeg_quality,
                ),
                "image/jpeg",
            )

    return load_image_asset(
        task.asset.local_path,
        max_side=args.max_side,
        jpeg_quality=args.jpeg_quality,
    )


def process_page_task(
    task: PageTask,
    args: argparse.Namespace,
    get_client,
    types_module,
    rate_limiter: RequestRateLimiter,
) -> tuple[dict[str, object], str]:
    try:
        image_bytes, mime_type = render_page_task(task, args)
        prompt = build_prompt(task.asset, task.page_number, task.page_count)
        body = gemini_page_markdown(
            get_client(),
            types_module,
            model=args.model,
            thinking_level=args.thinking_level,
            prompt=prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
            temperature=args.temperature,
            retries=args.retries,
            retry_sleep=args.retry_sleep,
            rate_limiter=rate_limiter,
        )
        atomic_write_text(
            task.output_path,
            build_markdown_file(
                task.asset,
                page_number=task.page_number,
                page_count=task.page_count,
                model=args.model,
                body=body,
            ),
        )
        if args.sleep:
            time.sleep(args.sleep)
        return (
            {
                "status": "ok",
                "asset": task.asset.slug,
                "source_file": str(task.asset.local_path),
                "page": task.page_number,
                "page_count": task.page_count,
                "output": str(task.output_path),
                "chars": len(body),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            f"wrote {task.asset.slug} page {task.page_number}: {task.output_path}",
        )
    except Exception as exc:  # noqa: BLE001 - preserve page-level failures in long batch runs.
        error_path = task.output_path.with_suffix(".error.txt")
        atomic_write_text(error_path, f"{type(exc).__name__}: {exc}\n")
        return (
            {
                "status": "error",
                "asset": task.asset.slug,
                "source_file": str(task.asset.local_path),
                "page": task.page_number,
                "page_count": task.page_count,
                "error": f"{type(exc).__name__}: {exc}",
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            f"error {task.asset.slug} page {task.page_number}: {exc}",
        )


def process_page_tasks(tasks: list[PageTask], args: argparse.Namespace, api_key: str) -> None:
    from google import genai
    from google.genai import types

    manifest_path = args.output_dir / "manifest.jsonl"
    rate_limiter = RequestRateLimiter(args.rpm)
    thread_local = threading.local()

    def get_client():
        client = getattr(thread_local, "client", None)
        if client is None:
            client = genai.Client(api_key=api_key)
            thread_local.client = client
        return client

    total = len(tasks)
    if total == 0:
        print("No page work queued after resume checks.")
        return

    workers = max(1, min(args.workers, total))
    print(f"Queued {total} page(s). workers={workers}, rpm={args.rpm}")

    if workers == 1:
        for index, task in enumerate(tasks, start=1):
            record, message = process_page_task(task, args, get_client, types, rate_limiter)
            append_jsonl(manifest_path, record)
            print(f"[{index}/{total}] {message}", flush=True)
            if record["status"] == "error" and args.stop_on_error:
                raise RuntimeError(message)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_task = {
            executor.submit(process_page_task, task, args, get_client, types, rate_limiter): task
            for task in tasks
        }
        for index, future in enumerate(concurrent.futures.as_completed(future_to_task), start=1):
            task = future_to_task[future]
            try:
                record, message = future.result()
            except Exception as exc:  # noqa: BLE001 - catch unexpected thread failures.
                message = f"error {task.asset.slug} page {task.page_number}: {exc}"
                record = {
                    "status": "error",
                    "asset": task.asset.slug,
                    "source_file": str(task.asset.local_path),
                    "page": task.page_number,
                    "page_count": task.page_count,
                    "error": f"{type(exc).__name__}: {exc}",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

            append_jsonl(manifest_path, record)
            print(f"[{index}/{total}] {message}", flush=True)
            if record["status"] == "error" and args.stop_on_error:
                for pending in future_to_task:
                    pending.cancel()
                raise RuntimeError(message)


def dry_run(assets: Iterable[Asset], skipped: list[dict[str, str]], args: argparse.Namespace) -> None:
    print("Dry run. No Gemini calls will be made.")
    for asset in assets:
        if asset.local_path.suffix.lower() == ".pdf":
            try:
                import fitz

                with fitz.open(asset.local_path) as doc:
                    pages = selected_pages(doc.page_count, args.pages, args.max_pages_per_doc)
                    print(f"{asset.slug}: {asset.local_path} ({len(pages)}/{doc.page_count} pages)")
            except Exception as exc:  # noqa: BLE001
                print(f"{asset.slug}: {asset.local_path} (page count error: {exc})")
        else:
            print(f"{asset.slug}: {asset.local_path} (image)")

    if skipped:
        print(f"\nSkipped metadata rows: {len(skipped)}")
        for item in skipped[:20]:
            print(f"  row {item['row']}: {item['title']!r} - {item['reason']}")
        if len(skipped) > 20:
            print(f"  ... {len(skipped) - 20} more")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Process the UFO-USA dataset page by page with Gemini and write Markdown files.",
    )
    parser.add_argument("--metadata", type=Path, default=Path("metadata/uap-csv.csv"))
    parser.add_argument("--pdf-manifest", type=Path, default=Path("metadata/pdf_manifest.tsv"))
    parser.add_argument("--downloads-dir", type=Path, default=Path("downloads/war-gov-ufo-release-1"))
    parser.add_argument("--output-dir", type=Path, default=Path("converted"))
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--temperature",
        type=float,
        default=float(os.environ.get("GEMINI_TEMPERATURE", DEFAULT_TEMPERATURE)),
        help="Gemini generation temperature. Defaults to 0 for deterministic transcription.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("GEMINI_WORKERS", DEFAULT_WORKERS)),
        help="Parallel page workers. Also configurable with GEMINI_WORKERS.",
    )
    parser.add_argument(
        "--rpm",
        type=int,
        default=int(os.environ.get("GEMINI_RPM", DEFAULT_RPM)),
        help="Global Gemini request-per-minute gate. Use 0 to disable.",
    )
    parser.add_argument("--thinking-level", default="HIGH", help='Use "none" to disable thinking_config.')
    parser.add_argument("--download-missing", action="store_true", help="Download missing assets from metadata URLs.")
    parser.add_argument("--local-only", action="store_true", help="Ignore metadata and process downloaded files only.")
    parser.add_argument("--force", action="store_true", help="Regenerate pages even when Markdown output exists.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop the batch after the first page error.")
    parser.add_argument("--dry-run", action="store_true", help="List files/pages without calling Gemini.")
    parser.add_argument("--pages", default="", help='Page selection like "1", "1-5", or "1,4,9-12".')
    parser.add_argument("--max-docs", type=int, default=0, help="Limit number of assets for smoke tests.")
    parser.add_argument("--max-pages-per-doc", type=int, default=0, help="Limit pages per asset for smoke tests.")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-side", type=int, default=3000, help="Resize page image so the longest side is at most this many pixels.")
    parser.add_argument("--jpeg-quality", type=int, default=85)
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to wait after each successful Gemini page call.")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.rpm < 0:
        parser.error("--rpm must be 0 or greater")

    args.output_dir = args.output_dir.resolve()
    args.downloads_dir = args.downloads_dir.resolve()
    args.metadata = args.metadata.resolve()
    args.pdf_manifest = args.pdf_manifest.resolve()

    skipped: list[dict[str, str]] = []
    if args.local_only:
        assets = read_local_assets(args.downloads_dir)
    elif args.pdf_manifest.exists():
        assets, skipped = read_pdf_manifest_assets(
            args.pdf_manifest,
            args.downloads_dir,
            download_missing=args.download_missing,
        )
    elif args.metadata.exists():
        assets, skipped = read_metadata_assets(
            args.metadata,
            args.downloads_dir,
            download_missing=args.download_missing,
        )
    else:
        assets = read_local_assets(args.downloads_dir)

    if args.max_docs:
        assets = assets[: args.max_docs]

    if not assets:
        print("No processable assets found.", file=sys.stderr)
        if skipped:
            dry_run([], skipped, args)
        return 1

    if args.dry_run:
        dry_run(assets, skipped, args)
        return 0

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Set GEMINI_API_KEY in the environment before running.", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Processing {len(assets)} asset(s) into {args.output_dir}")
    tasks = build_page_tasks(assets, args)
    process_page_tasks(tasks, args, api_key)

    if skipped:
        append_jsonl(
            args.output_dir / "manifest.jsonl",
            {
                "status": "skipped_metadata_rows",
                "count": len(skipped),
                "items": skipped,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print(f"Skipped {len(skipped)} metadata row(s); see manifest.jsonl.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
