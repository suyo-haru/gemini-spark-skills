#!/usr/bin/env python3
"""Markdownファイルの文字数・空白文字数・段落数を集計する。

実行例:
    python wordcount.py document.md
    python wordcount.py document.md --line 1-5
    python wordcount.py document.md --line 10

必要環境: Python 3.11 / markdown-it-py 4.0.0（提供された環境レポート準拠）。
日本語の形態素解析パッケージがレポートにないため、単語数は集計しない。

集計仕様:
* 入力はUTF-8（BOM付き可）。BOMは除き、CRLF / CRはLFに統一する。
* 文字数はPythonのlen()によるUnicodeコードポイント数。Markdown記号、
  HTMLタグ、空白、改行を含む。結合文字・絵文字は表示上の1文字と一致しない。
* WhiteSpaceはstr.isspace()で判定する。全角スペース、タブ、LF等も含む。
* コメント除外は、MarkdownでHTMLとして認識された<!-- ... -->が対象。
  コード内やエスケープされた表記、HTML属性値内の文字列は残す。
  閉じていないHTMLコメントブロックは、そのブロックの末尾まで除外する。
* 段落はCommonMarkのparagraph_openを数える。リスト・引用内も含み、
  見出し、コードブロック、HTMLブロックは含めない。GFM拡張は使用しない。
* 行番号は1始まり・両端を含む。全文を解析してから指定範囲を集計する。
  範囲に一部でもかかる段落を1段落と数えるため、範囲別の段落数は加算不可。
  コメントを除去してMarkdownを再解析するわけではない。
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


def line_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)(?:-([1-9][0-9]*))?", value)
    if not match:
        raise argparse.ArgumentTypeError("行指定は1以上の整数、または開始-終了（例: 1-5）")
    first = int(match[1])
    last = int(match[2] or match[1])
    if first > last:
        raise argparse.ArgumentTypeError("開始行は終了行以下にすること")
    return first, last


@dataclass(frozen=True)
class Counts:
    characters: int
    characters_without_comments: int
    whitespace: int
    whitespace_without_comments: int
    paragraphs: int


class HTMLComments(HTMLParser):
    """HTML属性値・script等と区別し、HTMLブロック内のコメント位置を得る。"""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.starts = [0] + [m.end() for m in re.finditer("\n", source)]
        self.spans: list[tuple[int, int]] = []
        self.feed(source)
        # feed後に残る未完のコメント。close()すると通常テキストとして処理される。
        if self.rawdata.startswith("<!--"):
            row, column = self.getpos()
            self.spans.append((self.starts[row - 1] + column, len(source)))

    def handle_comment(self, data: str) -> None:
        row, column = self.getpos()
        start = self.starts[row - 1] + column
        close = start + 4 + len(data)
        # CommonMarkと同じく、標準の --> を終端として扱う。
        if self.source.startswith("-->", close):
            self.spans.append((start, close + 3))


def make_parser():
    from markdown_it import MarkdownIt
    from markdown_it.rules_inline.html_inline import html_inline

    def track_comment(state, silent):
        start = state.pos
        matched = html_inline(state, silent)
        if matched and not silent:
            token = state.tokens[-1]
            if token.content.startswith("<!--"):
                token.meta["comment_span"] = (start, state.pos)
        return matched

    parser = MarkdownIt("commonmark")
    parser.inline.ruler.at("html_inline", track_comment)
    return parser


def source_position(token, position: int, lines: list[str], starts: list[int]) -> int:
    """トークン内のコメント端点を元の文書へ戻す（引用・リストの字下げ対応）。"""
    row = token.content.count("\n", 0, position)
    row_start = token.content.rfind("\n", 0, position) + 1
    row_end = token.content.find("\n", position)
    if row_end == -1:
        row_end = len(token.content)
    content_line = token.content[row_start:row_end]
    # パーサーがタブの途中から生成する仮想的な字下げを取り除く。
    needle = content_line.lstrip(" \t")
    indentation = len(content_line) - len(needle)
    source_row = token.map[0] + row
    # CommonMarkはNULをU+FFFDへ置換するが、置換前後の長さは同じ。
    column = lines[source_row].replace("\x00", "\ufffd").find(needle)
    if column == -1:
        raise ValueError("コメント位置を元の行へ対応付けできない")
    return starts[source_row] + column + position - row_start - indentation


def comment_spans(tokens, lines: list[str], starts: list[int]):
    for token in tokens:
        if token.type == "html_block":
            spans = HTMLComments(token.content).spans
        elif token.type == "inline":
            spans = [
                child.meta["comment_span"]
                for child in token.children or []
                if "comment_span" in child.meta
            ]
        else:
            continue
        for first, last in spans:
            begin = source_position(token, first, lines, starts)
            if token.content[first:last].endswith("-->"):
                end = source_position(token, last - 3, lines, starts) + 3
            else:
                end = starts[token.map[1]]
            yield begin, end


def count_markdown(text: str, selected: tuple[int, int] | None = None) -> Counts:
    # readline()と同じく、LFだけを行境界とする。U+2028等では分割しない。
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    if starts[-1] != len(text):
        starts.append(len(text))
    lines = [text[a:b] for a, b in zip(starts, starts[1:])]
    first, last = selected if selected is not None else (1, len(lines))
    if selected is not None and last > len(lines):
        raise ValueError(f"指定範囲 {first}-{last} は全 {len(lines)} 行の範囲外")
    begin = starts[first - 1] if lines else 0
    end = starts[last] if lines else 0
    tokens = make_parser().parse(text)

    # 全文内のコメントを選択範囲で切り詰める。範囲の途中に開閉記号がなくてもよい。
    spans = sorted(
        (max(begin, a), min(end, b))
        for a, b in comment_spans(tokens, lines, starts)
        if a < end and b > begin
    )
    pieces = []
    cursor = begin
    for a, b in spans:
        if a > cursor:
            pieces.append(text[cursor:a])
        cursor = max(cursor, b)
    pieces.append(text[cursor:end])
    cleaned = "".join(pieces)
    original = text[begin:end]
    paragraphs = sum(
        1 for token in tokens
        if token.type == "paragraph_open"
        and token.map[0] < last
        and token.map[1] > first - 1
    )
    return Counts(
        len(original), len(cleaned),
        sum(c.isspace() for c in original),
        sum(c.isspace() for c in cleaned), paragraphs,
    )


def main() -> int:
    cli = argparse.ArgumentParser(
        description="Markdownの文字数・空白文字数・CommonMark段落数を表示する。",
        epilog="UTF-8入力。BOMは除外し、CRLF/CRはLF（1文字）に統一。改行も文字数に含む。",
        allow_abbrev=False,
    )
    cli.add_argument("path", type=Path, help="Markdownファイルのパス")
    cli.add_argument("--line", type=line_range, metavar="N|N-M", help="集計する行（既定: 全行）")
    args = cli.parse_args()
    try:
        with args.path.open("r", encoding="utf-8-sig", newline=None) as stream:
            text = stream.read()
        counts = count_markdown(text, args.line)
    except ModuleNotFoundError as exc:
        cli.exit(2, f"エラー: 必要なパッケージ {exc.name!r} が見つからない。"
                 "環境レポート記載のmarkdown-it-py 4.0.0があるPythonで実行すること。\n")
    except (OSError, UnicodeError, ValueError) as exc:
        cli.exit(2, f"エラー: {exc}\n")

    selected = f"{args.line[0]}-{args.line[1]}行" if args.line else "全行"
    print(f"ファイル: {args.path}")
    print(f"対象範囲: {selected}")
    print(f"文字数（記号・空白・改行を含む）: {counts.characters:,}")
    print(f"文字数（HTMLコメント除外）     : {counts.characters_without_comments:,}")
    print(f"WhiteSpace文字数               : {counts.whitespace:,}")
    print(f"WhiteSpace文字数（コメント除外）: {counts.whitespace_without_comments:,}")
    print(f"段落数（CommonMark）           : {counts.paragraphs:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
