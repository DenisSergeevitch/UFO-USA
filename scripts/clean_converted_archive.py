#!/usr/bin/env python3
"""Deterministically clean public converted Markdown archive artifacts."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


SOURCE_FILE_LINE_RE = re.compile(
    r'(?m)^(?P<prefix>source_file:\s*)"(?P<value>/[^"\n]*/downloads/war-gov-ufo-release-1/[^"\n]+)"\s*$'
)

REPETITION_PATTERNS: tuple[tuple[str, re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (
        "html_nbsp_runs",
        re.compile(r"(?:&nbsp;[ \t]*){80,}", re.IGNORECASE),
        " ",
    ),
    (
        "html_br_line_runs",
        re.compile(r"(?im)(?:^[ \t]*<br\s*/?>[ \t]*\n){20,}"),
        "\n",
    ),
    (
        "bracket_marker_runs",
        re.compile(r"(?i)(\[(?:illegible|unreadable|unclear|redacted|blank)\])(?:\s*\1){19,}"),
        lambda match: match.group(1),
    ),
    (
        "dot_runs",
        re.compile(r"\.{100,}"),
        "...",
    ),
    (
        "dash_runs",
        re.compile(r"-{100,}"),
        "---",
    ),
    (
        "underscore_runs",
        re.compile(r"_{100,}"),
        "___",
    ),
    (
        "equals_runs",
        re.compile(r"={100,}"),
        "===",
    ),
)
BRACKET_MARKER_PREFIXES = sorted(
    {
        word[:index]
        for word in ("illegible", "unreadable", "unclear", "redacted", "blank")
        for index in range(0, len(word) + 1)
    },
    key=len,
    reverse=True,
)
BRACKET_MARKER_WORDS = "illegible|unreadable|unclear|redacted|blank"
TRAILING_PARTIAL_AFTER_MARKER_RE = re.compile(
    r"(?i)(\[(?:" + BRACKET_MARKER_WORDS + r")\])\s+\[(?:"
    + "|".join(re.escape(prefix) for prefix in BRACKET_MARKER_PREFIXES)
    + r")?[ \t]*$"
)
TRAILING_PARTIAL_BRACKET_RE = re.compile(
    r"(?i)[ \t]*\[(?:" + "|".join(re.escape(prefix) for prefix in BRACKET_MARKER_PREFIXES) + r")?[ \t]*$"
)
TRAILING_PARTIAL_NBSP_RE = re.compile(r"(?i)[ \t]*&(?:nbsp|nbs|nb|n)?;?[ \t]*$")
TRAILING_PARTIAL_NBSP_LINE_RE = re.compile(r"(?i)(?:^|\n)[ \t]*&(?:nbsp|nbs|nb|n)?;?[ \t]*$")


@dataclass(frozen=True)
class FileChange:
    path: Path
    before_chars: int
    after_chars: int
    changes: dict[str, int]


def publicize_path_string(value: str) -> str:
    replacements = (
        ("/downloads/war-gov-ufo-release-1/", "downloads/war-gov-ufo-release-1/"),
        ("/converted/", "converted/"),
    )
    for private_segment, public_prefix in replacements:
        if value.startswith("/") and private_segment in value:
            return public_prefix + value.split(private_segment, 1)[1]
    return value


def split_front_matter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return "", text
    return text[: end + len("\n---\n")], text[end + len("\n---\n") :]


def scrub_markdown_paths(text: str, changes: Counter[str]) -> str:
    def replace_source_file(match: re.Match[str]) -> str:
        old_value = match.group("value")
        new_value = publicize_path_string(old_value)
        if new_value != old_value:
            changes["private_source_paths"] += 1
        return f"{match.group('prefix')}{json.dumps(new_value, ensure_ascii=False)}"

    return SOURCE_FILE_LINE_RE.sub(replace_source_file, text)


def clean_repetitions(body: str, changes: Counter[str]) -> str:
    cleaned = body
    for name, pattern, replacement in REPETITION_PATTERNS:
        before_len = len(cleaned)
        cleaned, count = pattern.subn(replacement, cleaned)
        if count:
            changes[name] += count
            changes[f"{name}_chars_removed"] += before_len - len(cleaned)

    before_len = len(cleaned)
    cleaned, count = TRAILING_PARTIAL_AFTER_MARKER_RE.subn(lambda match: match.group(1), cleaned)
    if count:
        changes["trailing_partial_bracket_marker"] += count
        changes["trailing_partial_bracket_marker_chars_removed"] += before_len - len(cleaned)

    before_len = len(cleaned)
    cleaned, count = TRAILING_PARTIAL_NBSP_LINE_RE.subn(lambda match: "\n" if match.group(0).startswith("\n") else "", cleaned)
    if count:
        changes["trailing_partial_nbsp"] += count
        changes["trailing_partial_nbsp_chars_removed"] += before_len - len(cleaned)

    artifact_runs = sum(changes[name] for name, _, _ in REPETITION_PATTERNS)
    if artifact_runs:
        before_len = len(cleaned)
        cleaned, count = TRAILING_PARTIAL_BRACKET_RE.subn("", cleaned)
        if count:
            changes["trailing_partial_bracket_marker"] += count
            changes["trailing_partial_bracket_marker_chars_removed"] += before_len - len(cleaned)

        before_len = len(cleaned)
        cleaned, count = TRAILING_PARTIAL_NBSP_RE.subn("", cleaned)
        if count:
            changes["trailing_partial_nbsp"] += count
            changes["trailing_partial_nbsp_chars_removed"] += before_len - len(cleaned)

    before_len = len(cleaned)
    cleaned, whitespace_lines = re.subn(r"(?m)^[ \t]+$", "", cleaned)
    if whitespace_lines:
        changes["whitespace_only_lines"] += whitespace_lines
        changes["whitespace_only_lines_chars_removed"] += before_len - len(cleaned)

    return cleaned


def clean_markdown_text(text: str) -> tuple[str, dict[str, int]]:
    changes: Counter[str] = Counter()
    text = scrub_markdown_paths(text, changes)
    front_matter, body = split_front_matter(text)
    cleaned = front_matter + clean_repetitions(body, changes)
    return cleaned, dict(changes)


def clean_manifest_text(text: str) -> tuple[str, dict[str, int]]:
    changes: Counter[str] = Counter()
    output_lines: list[str] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            output_lines.append(line)
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at manifest line {line_number}: {exc}") from exc

        for key in ("source_file", "output"):
            value = record.get(key)
            if not isinstance(value, str):
                continue
            new_value = publicize_path_string(value)
            if new_value != value:
                record[key] = new_value
                changes[f"manifest_{key}_paths"] += 1

        output_lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))

    return "\n".join(output_lines) + ("\n" if text.endswith("\n") else ""), dict(changes)


def scan_markdown(converted_dir: Path, apply: bool) -> list[FileChange]:
    file_changes: list[FileChange] = []
    for path in sorted(converted_dir.rglob("*.md")):
        old_text = path.read_text(encoding="utf-8")
        new_text, changes = clean_markdown_text(old_text)
        if old_text == new_text:
            continue
        if apply:
            path.write_text(new_text, encoding="utf-8")
        file_changes.append(
            FileChange(path=path, before_chars=len(old_text), after_chars=len(new_text), changes=changes)
        )
    return file_changes


def clean_manifest(manifest_path: Path, apply: bool) -> FileChange | None:
    if not manifest_path.exists():
        return None
    old_text = manifest_path.read_text(encoding="utf-8")
    new_text, changes = clean_manifest_text(old_text)
    if old_text == new_text:
        return None
    if apply:
        manifest_path.write_text(new_text, encoding="utf-8")
    return FileChange(path=manifest_path, before_chars=len(old_text), after_chars=len(new_text), changes=changes)


def print_report(file_changes: list[FileChange], manifest_change: FileChange | None, apply: bool) -> None:
    all_changes = list(file_changes)
    if manifest_change is not None:
        all_changes.append(manifest_change)

    mode = "updated" if apply else "would update"
    print(f"{mode} {len(all_changes)} file(s)")

    totals: Counter[str] = Counter()
    chars_removed = 0
    for item in all_changes:
        totals.update(item.changes)
        chars_removed += item.before_chars - item.after_chars

    if totals:
        print("changes:")
        for key, value in sorted(totals.items()):
            if key.endswith("_chars_removed"):
                continue
            print(f"  {key}: {value}")
    print(f"chars_removed: {chars_removed}")

    for item in sorted(all_changes, key=lambda change: change.before_chars - change.after_chars, reverse=True)[:30]:
        delta = item.before_chars - item.after_chars
        print(f"  {delta:8d} chars  {item.path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrub private local paths and collapse obvious repeated-token artifacts in converted Markdown.",
    )
    parser.add_argument("--converted-dir", type=Path, default=Path("converted"))
    parser.add_argument("--manifest", type=Path, default=Path("converted/manifest.jsonl"))
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this flag, only report.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    file_changes = scan_markdown(args.converted_dir, args.apply)
    manifest_change = clean_manifest(args.manifest, args.apply)
    print_report(file_changes, manifest_change, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
