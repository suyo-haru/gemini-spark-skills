#!/usr/bin/env python3
"""通常のMarkdownコードフェンスを除外して、文字種の混入を確認する。"""

from __future__ import annotations

import argparse
import codecs
import io
import json
import re
import stat
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


# Unicode 17.0.0のブロックを基礎にした簡易チェック。
# https://www.unicode.org/Public/17.0.0/ucd/Blocks.txt
# LATINは一般的な文字範囲に限定し、×・÷を含めない。
# ARABICにはブロック内の数字・記号も含めるが、U+FEFFは除外する。
SCRIPT_RANGES = {
    "LATIN": (
        r"\u0041-\u005A\u0061-\u007A"
        r"\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u024F"
        r"\u1E00-\u1EFF\uFF21-\uFF3A\uFF41-\uFF5A"
    ),
    "HANGUL": (
        r"\u1100-\u11FF\u302E-\u302F\u3130-\u318F"
        r"\uA960-\uA97F\uAC00-\uD7AF\uD7B0-\uD7FF\uFFA0-\uFFDC"
    ),
    "ARABIC": (
        r"\u0600-\u06FF\u0750-\u077F\u0870-\u089F\u08A0-\u08FF"
        r"\uFB50-\uFDFF\uFE70-\uFEFE"
        r"\U00010EC0-\U00010EFF\U0001EE00-\U0001EEFF"
    ),
}


def build_pattern(
    *, latin: bool = True, hangul: bool = True, arabic: bool = True
) -> re.Pattern[str] | None:
    """有効な分類をまとめた正規表現を作る。全分類無効ならNone。"""
    enabled = {"LATIN": latin, "HANGUL": hangul, "ARABIC": arabic}
    branches = [
        f"(?P<{name}>[{ranges}]+)"
        for name, ranges in SCRIPT_RANGES.items()
        if enabled[name]
    ]
    return re.compile("|".join(branches)) if branches else None


DEFAULT_PATTERN = build_pattern()
OPEN_FENCE = re.compile(r" {0,3}(?P<fence>\x60{3,}|~{3,})(?P<info>.*)")


@dataclass(frozen=True, slots=True)
class Finding:
    line: int
    column: int
    script: str
    text: str


@dataclass(slots=True)
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    unclosed_fence_line: int | None = None


@dataclass(slots=True)
class FileResult:
    path: str
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error is not None:
            return "error"
        return "found" if self.findings else "clean"


def check_text(
    text: str, pattern: re.Pattern[str] | None = DEFAULT_PATTERN
) -> ScanResult:
    """本文を検査する。行・列は1始まり、列はコードポイント数。"""
    result = ScanResult()
    closing_fence: re.Pattern[str] | None = None
    opening_line: int | None = None

    # splitlines()とは異なり、垂直タブ等は改行として扱わない。
    # ファイル入力と同じく、LF・CRLF・CRだけを行区切りとして扱う。
    with io.StringIO(text, newline=None) as lines:
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.removesuffix("\n")
            if closing_fence is not None:
                if closing_fence.fullmatch(line):
                    closing_fence = None
                    opening_line = None
                continue

            opening = OPEN_FENCE.fullmatch(line)
            if opening is not None:
                fence = opening["fence"]
                # バッククォートの情報文字列には同じ記号を許可しない。
                if fence[0] != "\x60" or "\x60" not in opening["info"]:
                    closing_fence = re.compile(
                        r" {0,3}"
                        + re.escape(fence[0])
                        + "{"
                        + str(len(fence))
                        + r",}[ \t]*"
                    )
                    opening_line = line_number
                    continue

            if pattern is not None:
                for match in pattern.finditer(line):
                    result.findings.append(
                        Finding(
                            line=line_number,
                            column=match.start() + 1,
                            script=match.lastgroup or "",
                            text=match.group(),
                        )
                    )

    result.unclosed_fence_line = opening_line
    return result


def check_file(
    path: Path,
    *,
    encoding: str = "utf-8-sig",
    pattern: re.Pattern[str] | None = DEFAULT_PATTERN,
    display_path: str | None = None,
) -> FileResult:
    """ファイル全体を読み取り、読み取り失敗と未閉鎖フェンスを報告する。"""
    display = str(path) if display_path is None else display_path
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return FileResult(display, error="通常のファイルではない")
        text = path.read_text(encoding=encoding, errors="strict")
    except (OSError, UnicodeError, LookupError, ValueError) as exc:
        return FileResult(display, error=f"読み取り失敗: {exc}")

    scan = check_text(text, pattern)
    error = None
    if scan.unclosed_fence_line is not None:
        error = (
            f"{scan.unclosed_fence_line}行目で開始したコードフェンスが"
            "閉じられないままEOFに到達した"
        )
    return FileResult(display, findings=scan.findings, error=error)


def build_json_report(results: list[FileResult]) -> dict[str, object]:
    failed = sum(result.error is not None for result in results)
    flagged = sum(result.status == "found" for result in results)
    clean = sum(result.status == "clean" for result in results)
    matches = sum(len(result.findings) for result in results)
    exit_code = 2 if failed else (1 if matches else 0)
    return {
        "files": [
            {
                "path": result.path,
                "status": result.status,
                "findings": [asdict(finding) for finding in result.findings],
                "error": result.error,
            }
            for result in results
        ],
        "summary": {
            "checked": clean + flagged,
            "clean": clean,
            "flagged": flagged,
            "failed": failed,
            "matches": matches,
        },
        "exit_code": exit_code,
    }


def format_text(results: list[FileResult]) -> tuple[str, str]:
    """標準出力用と標準エラー出力用の文字列を返す。"""
    output: list[str] = []
    errors: list[str] = []
    for result in results:
        for finding in result.findings:
            quoted = json.dumps(finding.text, ensure_ascii=False)
            output.append(
                f"{result.path}:{finding.line}:{finding.column}: "
                f"[{finding.script}] {quoted}"
            )
        if result.status == "clean":
            output.append(f"OK: {result.path}")
        if result.error is not None:
            errors.append(f"ERROR: {result.path}: {result.error}")

    summary = build_json_report(results)["summary"]
    output.append(
        f"集計: 確認成功{summary['checked']}ファイル / "
        f"検出{summary['matches']}件 / エラー{summary['failed']}ファイル"
    )
    return "\n".join(output), "\n".join(errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "通常のMarkdownコードフェンスを除外して、"
            "ハングル・アラビア文字・英字系の混入を確認する。"
        )
    )
    parser.add_argument("files", nargs="+", help="チェックするテキストファイル")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--encoding", default="utf-8-sig", help="入力文字コード（既定: utf-8-sig）"
    )
    for name, label in (
        ("latin", "ASCII・全角・一般的なアクセント付き英字"),
        ("hangul", "ハングル"),
        ("arabic", "アラビア文字範囲（専用数字・記号を含む）"),
    ):
        parser.add_argument(
            f"--{name}",
            action=argparse.BooleanOptionalAction,
            default=True,
            help=f"{label}の検出を切り替える（既定: 有効）",
        )
    args = parser.parse_args(argv)
    try:
        codecs.lookup(args.encoding)
    except LookupError:
        parser.error(f"不明な文字コード: {args.encoding}")

    pattern = build_pattern(latin=args.latin, hangul=args.hangul, arabic=args.arabic)
    results: list[FileResult] = []
    seen: set[Path] = set()
    for display_path in args.files:
        path = Path(display_path)
        try:
            identity = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            results.append(FileResult(display_path, error=f"パス解決失敗: {exc}"))
            continue
        if identity in seen:
            continue
        seen.add(identity)
        results.append(
            check_file(
                path, encoding=args.encoding, pattern=pattern, display_path=display_path
            )
        )

    report = build_json_report(results)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        output, errors = format_text(results)
        print(output)
        if errors:
            print(errors, file=sys.stderr)
    return int(report["exit_code"])


if __name__ == "__main__":
    # リダイレクト時も、入力の文字コードにかかわらず出力はUTF-8に統一する。
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    raise SystemExit(main())
