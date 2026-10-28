#!/usr/bin/env python3
"""Replace exactly one match per edit in a UTF-8 file. Requires Python 3.10+."""

import argparse
import codecs
import os
from pathlib import Path
import stat
import sys
import tempfile


SEARCH = "<<<<<<< SEARCH"
SEPARATOR = "======="
REPLACE = ">>>>>>> REPLACE"
MARKERS = {SEARCH, SEPARATOR, REPLACE}


class EditError(ValueError):
    """An input cannot be applied unambiguously."""


def remove_line_ending(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def parse_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines(keepends=True)
    edits = []
    index = 0

    def read_body(end_marker: str) -> str:
        nonlocal index
        body = []
        while index < len(lines):
            line = lines[index]
            index += 1
            marker = remove_line_ending(line)
            if marker == end_marker:
                # One newline separates the payload from its closing marker.
                return remove_line_ending("".join(body))
            if marker in MARKERS:
                raise EditError(f"Line {index}: expected {end_marker!r}, got {marker!r}.")
            body.append(line)
        raise EditError(f"Missing marker: {end_marker}")

    while index < len(lines):
        line = remove_line_ending(lines[index])
        index += 1
        if not line.strip():
            continue
        if line != SEARCH:
            raise EditError(f"Line {index}: expected {SEARCH!r}.")
        search = read_body(SEPARATOR)
        replacement = read_body(REPLACE)
        edits.append((search, replacement))
    if not edits:
        raise EditError("No SEARCH / REPLACE blocks found.")
    return edits


def apply_edits(text: str, edits: list[tuple[str, str]]) -> str:
    for number, (search, replacement) in enumerate(edits, 1):
        if not search:
            raise EditError(f"Edit {number}: SEARCH must not be empty.")
        position = text.find(search)
        if position < 0:
            raise EditError(f"Edit {number}: SEARCH was not found.")
        # Count overlapping matches too: "aa" in "aaa" is ambiguous.
        if text.find(search, position + 1) >= 0:
            raise EditError(f"Edit {number}: SEARCH matches more than once; add context.")
        text = text[:position] + replacement + text[position + len(search):]
    return text


def adapt_block_newlines(
    edits: list[tuple[str, str]], text: str
) -> list[tuple[str, str]]:
    if "\r\n" not in text or "\n" in text.replace("\r\n", ""):
        return edits
    return [
        tuple(part.replace("\r\n", "\n").replace("\n", "\r\n") for part in edit)
        for edit in edits
    ]


def atomic_save(path: Path, original: bytes, updated: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        if path.read_bytes() != original:
            raise EditError("Target changed while preparing edits; retry against the current file.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="existing UTF-8 target file")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--search", help="exact, nonempty search text")
    source.add_argument("--blocks", metavar="PATH", help="UTF-8 block file, or - for stdin")
    parser.add_argument("--replace", help="replacement text; an empty string deletes the match")
    parser.add_argument("--dry-run", action="store_true", help="validate without saving")
    args = parser.parse_args(argv)
    if args.search is not None and args.replace is None:
        parser.error("--search requires --replace (which may be empty)")
    if args.blocks is not None and args.replace is not None:
        parser.error("--replace cannot be combined with --blocks")

    try:
        path = args.file.resolve(strict=True)
        original = path.read_bytes()
        bom = codecs.BOM_UTF8 if original.startswith(codecs.BOM_UTF8) else b""
        text = original[len(bom):].decode("utf-8")
        if args.blocks is not None:
            raw = sys.stdin.buffer.read() if args.blocks == "-" else Path(args.blocks).read_bytes()
            edits = adapt_block_newlines(parse_blocks(raw.decode("utf-8-sig")), text)
        else:
            edits = [(args.search, args.replace)]
        updated = bom + apply_edits(text, edits).encode("utf-8")
        if args.dry_run:
            print(f"OK: {len(edits)} edit(s) validated; file unchanged.")
        elif updated == original:
            print("OK: no change needed.")
        else:
            atomic_save(path, original, updated)
            print(f"OK: {len(edits)} edit(s) applied.")
        return 0
    except (EditError, OSError, UnicodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
