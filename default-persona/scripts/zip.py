#!/usr/bin/env python3
"""A practical, explicitly bounded Info-ZIP-style CLI for CPython 3.11+.

Uses only the standard library; never invokes an external zip/unzip program.
See README_zip.md for supported options and compatibility limits.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re
import shlex
import shutil
import stat
import struct
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime

VERSION = "1.0.0"
BUFFER = 1024 * 1024
HELP = """zip.py - practical Info-ZIP-style CLI (CPython 3.11+, standard library)
Usage: python zip.py [options] archive[.zip] [files ...] [-x patterns ...]
  -r  recurse paths          -R  recurse . and match patterns
  -u  add new/newer files     -f  freshen existing entries only
  -d  delete entries         -U  copy entries (requires --out)
  -j  junk paths             -D  omit directory entries
  -y  store symlinks          -0 .. -9  compression level (default 6)
  -Z store|deflate|bzip2      -n .zip:.gz  store these suffixes
  -i patterns ...            -x patterns ...   (@file lists accepted)
  -@  read paths from stdin  -nw  literal paths   -ws  * stops at /
  -t DATE / -tt DATE         include from / before YYYY-MM-DD or MMDDYYYY
  -q quiet   -v verbose      -MM  abort on missing/unreadable input
  -T  CRC-test before commit -m  remove archived source files after success
  -X  omit optional extras   -o  set archive time to latest entry time
  -z  archive comment        -c  comments for added/replaced entries
  -b DIR  temporary dir      -O FILE / --out FILE  output another archive
  -sf show selected names    -- ends option parsing
  -h / -h2 / --help          --version
Archive '-' writes ZIP bytes to stdout; file '-' reads bytes from stdin.
Supported options accept combined flags (e.g. -rq9), attached values, and
options after operands. ZIPOPT (or ZIP) supplies default options.
Unsupported options fail explicitly: encryption (-e/-P), splits (-s),
repair (-F/-FF), sync (-FS), differences (-DF), text conversion (-l/-ll),
and other options not listed here. This is NOT a complete Info-ZIP clone.
"""


class ZipError(Exception):
    def __init__(self, message: str, code: int = 16):
        super().__init__(message)
        self.code = code


class InputError(OSError):
    """An input failed before any output bytes for that member were written."""


class RawZipInfo(zipfile.ZipInfo):
    """Preserve legacy CP437 filename bytes in a copied central-directory entry."""
    __slots__ = ("raw_filename",)

    def _encodeFilenameFlags(self):
        return self.raw_filename, self.flag_bits


@dataclass
class Options:
    mode: str = "add"
    recurse: bool = False
    recurse_patterns: bool = False
    junk: bool = False
    no_dirs: bool = False
    symlinks: bool = False
    quiet: bool = False
    verbose: bool = False
    names_stdin: bool = False
    must_match: bool = False
    no_wild: bool = False
    stop_dirs: bool = False
    test: bool = False
    move: bool = False
    strip_extra: bool = False
    latest: bool = False
    archive_comment: bool = False
    entry_comments: bool = False
    show_files: bool = False
    level: int = 6
    method: int = zipfile.ZIP_DEFLATED
    suffixes: list[str] = field(default_factory=lambda: [
        ".Z", ".zip", ".zoo", ".arc", ".lzh", ".arj"
    ])
    includes: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    since: tuple | None = None
    before: tuple | None = None
    temp_dir: str | None = None
    output: str | None = None
    archive: str | None = None
    paths: list[str] = field(default_factory=list)
    action: str | None = None


# Long names follow zip 3.0's --show-options list. Only exact names accepted.
LONG = {
    "help": "h", "more-help": "h2", "version": "version",
    "recurse-paths": "r", "recurse-patterns": "R", "junk-paths": "j",
    "paths": "p", "no-dir-entries": "D", "symlinks": "y",
    "quiet": "q", "verbose": "v", "names-stdin": "@", "must-match": "MM",
    "no-wild": "nw", "wild-stop-dirs": "ws", "test": "T", "move": "m",
    "strip-extra": "X", "latest-time": "o", "archive-comment": "z",
    "entry-comments": "c", "show-files": "sf", "delete": "d",
    "update": "u", "freshen": "f", "copy": "U", "copy-entries": "U",
    "include": "i", "exclude": "x", "temp-path": "b", "temp-dir": "b",
    "output-file": "O", "out": "O", "compression-method": "Z",
    "suffixes": "n", "from-date": "t", "before-date": "tt", "store": "0",
    **{"compress-" + str(n): str(n) for n in range(1, 10)},
}
FLAGS = {
    "r": "recurse", "R": "recurse_patterns", "j": "junk", "D": "no_dirs",
    "y": "symlinks", "q": "quiet", "v": "verbose", "@": "names_stdin",
    "MM": "must_match", "nw": "no_wild", "ws": "stop_dirs", "T": "test",
    "m": "move", "X": "strip_extra", "o": "latest", "z": "archive_comment",
    "c": "entry_comments", "sf": "show_files",
}
VALUES = {"b", "O", "Z", "n", "t", "tt"}
MULTI = {"MM", "nw", "ws", "sf", "tt", "h2"}
UNSUPPORTED_MULTI = {
    "FS", "DF", "FF", "FI", "TT", "UN", "ll", "lf", "la", "li",
    "db", "dc", "dd", "dg", "ds", "du", "dv", "sp", "sv", "sb",
    "sc", "sd", "so", "su", "sU", "RE", "fz", "fd", "AS", "AC",
}


def date_arg(value: str) -> tuple:
    try:
        return datetime.strptime(
            value, "%Y-%m-%d" if "-" in value else "%m%d%Y"
        ).timetuple()[:6]
    except ValueError:
        raise ZipError("invalid date: " + value)


def read_list(value: str) -> list[str]:
    if not value.startswith("@"):
        return [value]
    if value == "@":
        raise ZipError("a list filename must follow @")
    try:
        with open(value[1:], encoding="utf-8", errors="surrogateescape") as f:
            return [line.rstrip("\r\n") for line in f if line.rstrip("\r\n")]
    except OSError as e:
        raise ZipError("cannot read list " + value[1:] + ": " + str(e), 18)


def parse(argv: list[str]) -> Options:
    o = Options()
    try:
        env = os.environ.get("ZIPOPT", os.environ.get("ZIP", ""))
        args = shlex.split(env) + list(argv)
    except ValueError as e:
        raise ZipError("invalid ZIPOPT/ZIP: " + str(e))
    operands = []
    i = 0
    literal = False
    selected_mode = None

    def apply(key: str, value: str | None = None):
        nonlocal selected_mode
        if key in FLAGS:
            setattr(o, FLAGS[key], True)
        elif key == "p":
            o.junk = False
        elif key in ("h", "h2", "H", "?"):
            o.action = "help"
        elif key == "version":
            o.action = "version"
        elif key in ("d", "u", "f", "U"):
            mode = {"d": "delete", "u": "update", "f": "freshen", "U": "copy"}[key]
            if selected_mode and selected_mode != mode:
                raise ZipError("conflicting operation modes")
            o.mode = selected_mode = mode
        elif key in "0123456789" and len(key) == 1:
            o.level = int(key)
            o.method = zipfile.ZIP_STORED if key == "0" else zipfile.ZIP_DEFLATED
        elif key == "b":
            o.temp_dir = value
        elif key == "O":
            o.output = value
        elif key == "n":
            o.suffixes = value.split(":") if value else []
        elif key in ("t", "tt"):
            setattr(o, "since" if key == "t" else "before", date_arg(value))
        elif key == "Z":
            methods = {"store": zipfile.ZIP_STORED, "deflate": zipfile.ZIP_DEFLATED,
                       "bzip2": zipfile.ZIP_BZIP2}
            if value not in methods:
                raise ZipError("unsupported compression method: " + value)
            o.method = methods[value]
        else:
            raise ZipError("unsupported option: -" + key)

    while i < len(args):
        arg = args[i]
        i += 1
        if literal or not arg.startswith("-") or arg == "-":
            operands.append(arg)
            continue
        if arg == "--":
            literal = True
            continue
        if arg.startswith("--"):
            name, equal, val = arg[2:].partition("=")
            if name not in LONG:
                raise ZipError("unsupported option: --" + name)
            key = LONG[name]
            if key not in VALUES | {"i", "x"} and equal:
                raise ZipError("option takes no value: --" + name)
            rest = val if equal else None
            keys = [(key, rest)]
        else:
            token = arg[1:]
            keys = []
            while token:
                prefix = token[:2]
                if prefix in UNSUPPORTED_MULTI:
                    raise ZipError("unsupported option: -" + prefix)
                key = prefix if prefix in MULTI else token[0]
                token = token[len(key):]
                if key in VALUES | {"i", "x"}:
                    keys.append((key, token.lstrip("=") if token else None))
                    break
                keys.append((key, None))
        for key, val in keys:
            if key in ("i", "x"):
                patterns = []
                if val is not None:
                    patterns.extend(read_list(val))
                while i < len(args) and not args[i].startswith("-"):
                    item = args[i]
                    i += 1
                    if item == "@":
                        break
                    patterns.extend(read_list(item))
                if not patterns:
                    raise ZipError("empty pattern list for -" + key)
                (o.includes if key == "i" else o.excludes).extend(patterns)
            else:
                if key in VALUES and val is None:
                    if i >= len(args):
                        raise ZipError("missing value for -" + key)
                    val = args[i]
                    i += 1
                apply(key, val)
    if operands:
        o.archive, *o.paths = operands
    if o.verbose and not operands and not o.action:
        o.action = "version"
    if not args:
        if sys.stdin.isatty():
            o.action = "help"
        else:
            o.archive, o.paths = "-", ["-"]
    if o.action:
        return o
    if not o.archive:
        raise ZipError("missing archive name")
    if o.recurse and o.recurse_patterns:
        raise ZipError("-r and -R cannot be combined")
    if o.mode == "copy" and not o.output:
        raise ZipError("-U requires --out")
    if o.mode in ("delete", "copy") and o.move:
        raise ZipError("-m requires a filesystem operation")
    if o.output and o.mode == "add" and not o.paths and not o.names_stdin:
        o.mode = "copy"
    if o.names_stdin:
        if "-" in o.paths:
            raise ZipError("-@ and file '-' both consume stdin")
        o.paths.extend(line.rstrip("\r\n") for line in sys.stdin
                       if line.rstrip("\r\n"))
    if (o.entry_comments or o.archive_comment) and (
            o.names_stdin or "-" in o.paths):
        raise ZipError("comments and file/list input cannot share stdin")
    if o.mode == "delete" and (o.includes or o.excludes):
        raise ZipError("delete mode does not accept -i/-x (as in Info-ZIP)")
    return o


def archive_path(path: str) -> str:
    if path == "-":
        return path
    return path if os.path.splitext(os.path.basename(path))[1] else path + ".zip"


def matches(name: str, pattern: str, o: Options) -> bool:
    if o.no_wild:
        return name == pattern
    if not o.stop_dirs:
        return fnmatch.fnmatchcase(name, pattern)
    # Translate wildcard tokens individually; character classes are delegated
    # to fnmatch so [!a], ranges and literal '[' have the same syntax.
    parts = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 2] == "**":
                parts.append(".*")
                i += 2
                continue
            parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        elif ch == "[":
            end = i + 1
            if end < len(pattern) and pattern[end] == "!":
                end += 1
            if end < len(pattern) and pattern[end] == "]":
                end += 1
            end = pattern.find("]", end)
            if end >= 0:
                translated = fnmatch.translate(pattern[i:end + 1])
                parts.append("(?!/)" + translated.removesuffix(r"\Z").removesuffix(r"\z"))
                i = end + 1
                continue
            parts.append(r"\[")
        else:
            parts.append(re.escape(ch))
        i += 1
    return re.fullmatch("".join(parts), name, flags=re.DOTALL) is not None


def selected(name: str, dt: tuple, o: Options) -> bool:
    return ((not o.includes or any(matches(name, p, o) for p in o.includes))
            and not any(matches(name, p, o) for p in o.excludes)
            and (o.since is None or dt >= o.since)
            and (o.before is None or dt < o.before))


def zip_datetime(timestamp: float) -> tuple:
    dt = time.localtime(timestamp)[:6]
    if dt[0] < 1980:
        return (1980, 1, 1, 0, 0, 0)
    if dt[0] > 2107:
        return (2107, 12, 31, 23, 59, 58)
    return dt[:5] + (dt[5] // 2 * 2,)


@dataclass
class Candidate:
    path: str
    name: str
    st: os.stat_result | None
    directory: bool = False
    link: bool = False

    @property
    def dt(self):
        return zip_datetime(self.st.st_mtime if self.st else time.time())


def identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


class Log:
    def __init__(self, o: Options, stream):
        self.o = o
        self.stream = stream
        self.read_error = False

    def info(self, text):
        if not self.o.quiet:
            print(text, file=self.stream)

    def warn(self, text, unreadable=False):
        if self.o.must_match:
            raise ZipError(text, 18)
        if unreadable:
            self.read_error = True
        print("zip warning: " + text, file=sys.stderr)


def gather(o: Options, blocked: set[str], log: Log) -> list[Candidate]:
    found: dict[str, Candidate] = {}
    operands = ["."] if o.recurse_patterns else o.paths
    recursive = o.recurse or o.recurse_patterns

    def add(path, ancestors: frozenset):
        if os.path.realpath(path) in blocked:
            return
        try:
            st = os.lstat(path) if o.symlinks else os.stat(path)
        except OSError as e:
            log.warn("cannot stat " + path + ": " + str(e), True)
            return
        directory = stat.S_ISDIR(st.st_mode)
        link = stat.S_ISLNK(st.st_mode)
        if not (directory or link or stat.S_ISREG(st.st_mode)):
            log.warn("skipping special file: " + path)
            return
        name = os.path.splitdrive(os.path.normpath(path))[1].replace(os.sep, "/")
        name = name.lstrip("/")
        if name == ".":
            name = ""
        if o.junk:
            name = os.path.basename(name)
        if directory and name:
            name += "/"
        c = Candidate(path, name, st, directory, link)
        permitted = bool(name) and not (directory and (o.no_dirs or o.junk))
        if o.recurse_patterns:
            permitted = permitted and any(matches(name, p, o) for p in o.paths)
        if permitted and selected(name, c.dt, o):
            previous = found.get(name)
            if previous and os.path.abspath(previous.path) != os.path.abspath(path):
                raise ZipError("two input paths map to the same ZIP name: " + name)
            found.setdefault(name, c)
        if directory and recursive:
            node = (st.st_dev, st.st_ino)
            if node in ancestors:
                log.warn("skipping directory symlink cycle: " + path)
                return
            try:
                with os.scandir(path) as it:
                    children = sorted(it, key=lambda e: e.name)
                for entry in children:
                    add(entry.path, ancestors | {node})
            except OSError as e:
                log.warn("cannot read directory " + path + ": " + str(e), True)

    if o.recurse_patterns and not o.paths:
        raise ZipError("-R requires at least one pattern")
    for operand in operands:
        if operand == "-" and not o.recurse_patterns:
            c = Candidate("-", "-", None)
            if selected(c.name, c.dt, o):
                found.setdefault("-", c)
            continue
        expanded = ([operand] if o.no_wild or os.path.lexists(operand)
                    else glob.glob(operand, include_hidden=True))
        if not expanded:
            log.warn("name not matched: " + operand)
        for path in expanded:
            add(path, frozenset())
    return list(found.values())


def bounded_copy(src, dst, length: int):
    while length:
        block = src.read(min(length, BUFFER))
        if not block:
            raise ZipError("unexpected end of existing archive", 3)
        dst.write(block)
        length -= len(block)


def copy_raw(zin: zipfile.ZipFile, zout: zipfile.ZipFile,
             info: zipfile.ZipInfo, ends: dict[int, int]):
    """Keep the local record, compressed payload and descriptor byte-for-byte.

    zipfile has no public raw-copy API. This small CPython-specific adapter
    touches filelist, NameToInfo, start_dir and _didModify; regression tests
    cover encrypted records, descriptors, Unicode and ZIP64 local headers.
    """
    end = ends[info.header_offset]
    if end <= info.header_offset:
        raise ZipError("overlapping entries in existing archive", 3)
    zin.fp.seek(info.header_offset)
    header = zin.fp.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ZipError("invalid local ZIP header", 3)
    name_length, extra_length = struct.unpack_from("<HH", header, 26)
    minimum = 30 + name_length + extra_length + info.compress_size
    if minimum > end - info.header_offset:
        raise ZipError("truncated/overlapping ZIP member: " + info.filename, 3)
    zin.fp.seek(info.header_offset)
    zout.fp.seek(zout.start_dir)
    new_info = RawZipInfo(info.filename, info.date_time)
    for attr in zipfile.ZipInfo.__slots__:
        if hasattr(info, attr):
            setattr(new_info, attr, getattr(info, attr))
    new_info.raw_filename = info.orig_filename.encode(
        "utf-8" if info.flag_bits & 0x800 else "cp437")
    new_info.header_offset = zout.fp.tell()
    bounded_copy(zin.fp, zout.fp, end - info.header_offset)
    zout.filelist.append(new_info)
    zout.NameToInfo[new_info.filename] = new_info
    zout.start_dir = zout.fp.tell()
    zout._didModify = True


def extra_fields(st: os.stat_result, strip: bool) -> bytes:
    if strip:
        return b""
    # Extended modification time and Unix UID/GID, also understood by unzip.
    timestamp = max(0, min(0xFFFFFFFF, int(st.st_mtime)))
    result = struct.pack("<HHBI", 0x5455, 5, 1, timestamp)
    uid = int(getattr(st, "st_uid", 0)).to_bytes(4, "little")
    gid = int(getattr(st, "st_gid", 0)).to_bytes(4, "little")
    body = b"\x01\x04" + uid + b"\x04" + gid
    return result + struct.pack("<HH", 0x7875, len(body)) + body


def prompt_entry(name: str) -> bytes:
    print("Enter comment for " + name + ":", file=sys.stderr, flush=True)
    value = sys.stdin.readline().rstrip("\r\n").encode("utf-8")
    if len(value) > 65535:
        raise ZipError("entry comment exceeds 65535 bytes")
    return value


def write_candidate(zout, c: Candidate, o: Options, comment: bytes):
    # Open before emitting a local header: unreadable inputs can be skipped
    # without leaving an incomplete member in the temporary ZIP.
    with ExitStack() as stack:
        st = c.st
        stream = None
        data = None
        try:
            if c.path == "-":
                stream = sys.stdin.buffer
            elif c.link:
                data = os.fsencode(os.readlink(c.path))
                if identity(os.lstat(c.path)) != identity(st):
                    raise ZipError("symlink changed while reading: " + c.path, 18)
            elif c.directory:
                current = os.stat(c.path)
                if (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
                    raise ZipError("directory replaced while scanning: " + c.path, 18)
            else:
                stream = stack.enter_context(open(c.path, "rb"))
                if identity(os.fstat(stream.fileno())) != identity(st):
                    raise ZipError("file changed while scanning: " + c.path, 18)
        except OSError as e:
            raise InputError(str(e)) from e
        info = zipfile.ZipInfo(c.name, c.dt)
        info.create_system = 3
        info.external_attr = ((st.st_mode if st else stat.S_IFREG | 0o600) << 16)
        if c.directory:
            info.external_attr |= 0x10
        info.extra = extra_fields(st, o.strip_extra) if st else b""
        info.comment = comment
        info.compress_type = o.method
        if c.directory or c.link or any(c.name.endswith(s) for s in o.suffixes if s):
            info.compress_type = zipfile.ZIP_STORED
        info._compresslevel = max(1, o.level) if o.method == zipfile.ZIP_BZIP2 else o.level
        info.file_size = (len(data) if data is not None else
                          st.st_size if st and not c.directory else 0)
        with zout.open(info, "w", force_zip64=(c.path == "-")) as dst:
            if data is not None:
                dst.write(data)
            elif stream is not None:
                shutil.copyfileobj(stream, dst, BUFFER)
        if stream is not None and c.path != "-":
            if identity(os.fstat(stream.fileno())) != identity(st):
                raise ZipError("file changed while archiving: " + c.path, 18)
        return info


def test_archive(path: str):
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise ZipError("CRC check failed: " + bad, 8)


def read_archive_comment() -> bytes:
    print('Enter archive comment (end with "." on its own line or EOF):',
          file=sys.stderr, flush=True)
    lines = []
    for line in sys.stdin:
        if line.rstrip("\r\n") == ".":
            break
        lines.append(line.rstrip("\r\n"))
    data = "\n".join(lines).encode("utf-8")
    if len(data) > 65535:
        raise ZipError("archive comment exceeds 65535 bytes")
    return data


def commit(temp: str, dest: str, mode: int):
    os.chmod(temp, mode)
    # -b may select a different filesystem. Stage beside the destination so
    # the final replacement is always atomic on the destination filesystem.
    if os.stat(temp).st_dev != os.stat(os.path.dirname(dest) or ".").st_dev:
        fd, stage = tempfile.mkstemp(prefix=".zip-stage-", dir=os.path.dirname(dest) or ".")
        try:
            with os.fdopen(fd, "wb") as out, open(temp, "rb") as src:
                shutil.copyfileobj(src, out, BUFFER)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(stage, mode)
            ts = os.stat(temp)
            os.utime(stage, ns=(ts.st_atime_ns, ts.st_mtime_ns))
            os.replace(stage, dest)
        finally:
            if os.path.exists(stage):
                os.unlink(stage)
    else:
        os.replace(temp, dest)


def remove_sources(written: list[Candidate], log: Log):
    for c in written:
        if c.path == "-" or c.directory:
            continue
        try:
            current = os.lstat(c.path) if c.link else os.stat(c.path)
            if identity(current) != identity(c.st):
                log.warn("source changed; not removed: " + c.path, True)
                continue
            os.unlink(c.path)
        except OSError as e:
            log.warn("cannot remove " + c.path + ": " + str(e), True)
    # Remove only selected directory entries, only if they are empty.
    directories = sorted((c for c in written if c.directory),
                         key=lambda c: c.path.count(os.sep), reverse=True)
    for c in directories:
        if os.path.islink(c.path):
            continue
        try:
            os.rmdir(c.path)
        except OSError:
            pass


def run(o: Options) -> int:
    source = archive_path(o.archive)
    dest = archive_path(o.output) if o.output else source
    if source == "-" and o.mode in ("delete", "copy", "freshen"):
        raise ZipError("this operation requires a seekable input archive")
    # Resolving existing output symlinks preserves their target semantics.
    if source != "-":
        source = os.path.realpath(source)
    if dest != "-":
        dest = os.path.realpath(dest)
    log = Log(o, sys.stderr if dest == "-" else sys.stdout)
    blocked = {p for p in (source, dest) if p != "-"}
    with ExitStack() as stack:
        zin = None
        initial = None
        if source != "-" and os.path.exists(source):
            initial = identity(os.stat(source))
            zin = stack.enter_context(zipfile.ZipFile(source))
        elif o.mode in ("delete", "copy", "freshen"):
            raise ZipError("archive not found: " + o.archive, 9)
        infos = zin.infolist() if zin else []
        if any(i.volume != 0 for i in infos):
            raise ZipError("split/multi-disk archives are unsupported", 16)
        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise ZipError("duplicate names in existing archive are unsupported", 3)
        by_name = {i.filename: i for i in infos}
        offsets = sorted(i.header_offset for i in infos)
        if len(set(offsets)) != len(offsets):
            raise ZipError("overlapping local records", 3)
        ends = {pos: offsets[n + 1] if n + 1 < len(offsets) else zin.start_dir
                for n, pos in enumerate(offsets)}
        candidates = []
        keep = set(names)
        changes = {}
        if o.mode in ("delete", "copy"):
            patterns = o.paths or (["*"] if o.mode == "copy" else [])
            chosen = {i.filename for i in infos
                      if any(matches(i.filename, p, o) for p in patterns)
                      and selected(i.filename, i.date_time, o)}
            for pattern in patterns:
                if not any(matches(n, pattern, o) for n in names):
                    log.warn("name not matched: " + pattern)
            if not chosen and o.mode == "delete":
                raise ZipError("nothing to do", 12)
            keep = chosen if o.mode == "copy" else keep - chosen
            action_names = [n for n in names if n in chosen]
        else:
            # zip -u/-f archive with no paths searches archive paths on disk.
            if not o.paths and o.mode in ("update", "freshen") and zin:
                o.paths = [n.rstrip("/") for n in names]
            candidates = gather(o, blocked, log)
            for c in candidates:
                old = by_name.get(c.name)
                if o.mode == "freshen" and old is None:
                    continue
                if o.mode in ("update", "freshen") and old and c.dt <= old.date_time:
                    continue
                changes[c.name] = c
            action_names = list(changes)
        if o.show_files:
            for name in action_names:
                print(name, file=log.stream)
            return 18 if log.read_error else 0
        comment_only = o.archive_comment and zin is not None
        test_only = o.test and zin is not None and not o.paths
        if o.mode not in ("copy", "delete") and not changes and not comment_only:
            if test_only:
                test_archive(source)
                log.info("test of " + o.archive + " OK")
                return 0
            raise ZipError("nothing to do", 12)
        if o.move and dest == "-":
            raise ZipError("-m cannot be combined with stdout output")
        if dest != "-" and not os.path.isdir(os.path.dirname(dest) or "."):
            raise ZipError("output directory does not exist", 15)
        destination_initial = identity(os.stat(dest)) if dest != "-" and os.path.exists(dest) else None
        dest_mode = stat.S_IMODE(os.stat(dest).st_mode) if destination_initial else None
        # Match normal file creation permissions, accounting for umask.
        mask = os.umask(0)
        os.umask(mask)
        if dest_mode is None:
            dest_mode = 0o666 & ~mask
        temp_dir = o.temp_dir or (os.path.dirname(dest) if dest != "-" else None)
        fd, temp = tempfile.mkstemp(prefix=".zip-tmp-", dir=temp_dir)
        os.close(fd)
        written = []
        try:
            with zipfile.ZipFile(temp, "w", allowZip64=True) as zout:
                # Preserve an existing SFX prefix when editing normal archives.
                if zin and infos and min(offsets) > 0:
                    zin.fp.seek(0)
                    bounded_copy(zin.fp, zout.fp, min(offsets))
                    zout.start_dir = zout.fp.tell()
                zout.comment = zin.comment if zin else b""
                for old in infos:
                    name = old.filename
                    if name in changes:
                        c = changes.pop(name)
                        comment = prompt_entry(name) if o.entry_comments else old.comment
                        try:
                            new = write_candidate(zout, c, o, comment)
                        except InputError as e:
                            log.warn("cannot read " + c.path + ": " + str(e), True)
                            copy_raw(zin, zout, old, ends)
                            continue
                        written.append(c)
                        log.info("  updating: " + name)
                        if o.verbose:
                            log.info("    %d -> %d bytes; CRC %08x" % (
                                new.file_size, new.compress_size, new.CRC))
                    elif name in keep:
                        copy_raw(zin, zout, old, ends)
                        if o.mode == "copy":
                            log.info("  copying: " + name)
                    elif o.mode == "delete":
                        log.info("deleting: " + name)
                for name, c in changes.items():
                    comment = prompt_entry(name) if o.entry_comments else b""
                    try:
                        new = write_candidate(zout, c, o, comment)
                    except InputError as e:
                        log.warn("cannot read " + c.path + ": " + str(e), True)
                        continue
                    written.append(c)
                    method = {0: "stored", 8: "deflated", 12: "bzip2"}[new.compress_type]
                    ratio = round(100 * (1 - new.compress_size / new.file_size)) if new.file_size else 0
                    log.info("  adding: " + name + " (" + method + " " + str(ratio) + "%)")
                    if o.verbose:
                        log.info("    %d -> %d bytes; CRC %08x" % (
                            new.file_size, new.compress_size, new.CRC))
                if o.archive_comment:
                    zout.comment = read_archive_comment()
            if not written and not infos and o.mode not in ("copy", "delete"):
                raise ZipError("nothing to do", 12)
            if o.test or o.move:
                test_archive(temp)
                log.info("test of " + o.archive + " OK")
            # Refuse a conflicting concurrent update before replacing either file.
            if initial is not None and identity(os.stat(source)) != initial:
                raise ZipError("input archive changed during operation", 18)
            if dest != "-":
                now = identity(os.stat(dest)) if os.path.exists(dest) else None
                if now != destination_initial:
                    raise ZipError("output archive changed during operation", 18)
            latest = None
            if o.latest:
                with zipfile.ZipFile(temp) as complete:
                    dates = [i.date_time for i in complete.infolist()]
                if dates:
                    latest = time.mktime(max(dates) + (0, 0, -1))
            # Close the input before atomic replacement (also works on Windows).
            if zin:
                zin.close()
            if dest == "-":
                with open(temp, "rb") as src:
                    shutil.copyfileobj(src, sys.stdout.buffer, BUFFER)
                sys.stdout.buffer.flush()
            else:
                if latest is not None:
                    os.utime(temp, (latest, latest))
                commit(temp, dest, dest_mode)
                if o.move and not log.read_error:
                    remove_sources(written, log)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return 18 if log.read_error else 0


def main(argv=None) -> int:
    try:
        o = parse(sys.argv[1:] if argv is None else argv)
        if o.action == "help":
            print(HELP)
            return 0
        if o.action == "version":
            print("zip.py " + VERSION + " (Info-ZIP-style subset; CPython "
                  + sys.version.split()[0] + "; zipfile; no external commands)")
            return 0
        return run(o)
    except ZipError as e:
        print("zip error: " + str(e), file=sys.stderr)
        return e.code
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as e:
        print("zip error: " + str(e), file=sys.stderr)
        return 3
    except (RuntimeError, NotImplementedError) as e:
        print("zip error: " + str(e), file=sys.stderr)
        return 16
    except ValueError as e:
        print("zip error: " + str(e), file=sys.stderr)
        return 16
    except BrokenPipeError:
        return 14
    except OSError as e:
        print("zip error: " + str(e), file=sys.stderr)
        return 15
    except KeyboardInterrupt:
        print("zip error: interrupted", file=sys.stderr)
        return 9


if __name__ == "__main__":
    sys.exit(main())
