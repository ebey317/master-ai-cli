#!/usr/bin/env python3
"""Read the documents the agent can already open.

The agent could always launch LibreOffice on a .docx, and could never read
one. Attaching a document hit the "does not look like a text file" path,
which told the user to convert it by hand using software that was already
installed. That is the gap this closes.

Every reader here uses something already on the box. No new dependencies:

    .docx            python-docx      (paragraphs and tables)
    .xlsx / .xlsm    openpyxl          (per sheet, formulas preserved)
    .xls             xlrd
    .pdf             pdftotext         (poppler)
    .doc / .rtf      catdoc
    .pptx .odt .ods  LibreOffice headless, converted to text
    .epub .html      stripped tags

LibreOffice is the universal fallback: it can open nearly anything, so an
unlisted format is attempted rather than refused. That is the difference
between "unsupported" and "we did not think of it".

Three failure modes drive the design, because each one silently produces a
wrong answer rather than an error:

1. A headless conversion is not always safe to run against the user's real
   profile. Older LibreOffice releases block indefinitely when a Writer or
   Calc window is already open, because the second process waits on the
   first one's profile lock, and a lock wait has no timeout -- so the agent
   deadlocks rather than failing. That hang did NOT reproduce on LibreOffice
   26.2.5.2, which handled a concurrent GUI instance in ~1s, so this is
   prevention rather than a live bug on this machine. Every invocation still
   gets a throwaway -env:UserInstallation directory, which costs a second
   and buys three things: immunity to that class of hang on older releases,
   no first-run wizard or extension side effects in the user's real
   profile, and no dependence on their LibreOffice settings.

2. A malformed file can hang a converter indefinitely. Every subprocess
   runs with a hard timeout and is killed on expiry.

3. An empty string is indistinguishable from "this document is empty".
   The model reads that as "the file has no content" and answers from
   nothing. So no reader returns "" on failure -- failures return a
   diagnostic, and callers can tell the two apart.
"""

from __future__ import annotations

import html.parser
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

MAX_CHARS = 60_000
MAX_BYTES = 200 * 1024 * 1024
# Per-attempt ceiling for one converter invocation.
SUBPROCESS_TIMEOUT = 25.0
# Ceiling for the whole LibreOffice fallback, across every format tried.
#
# The per-attempt timeout alone is not a bound: with three export formats
# to try, a pathological file could burn 3x the attempt ceiling and leave
# the agent apparently hung on a single document. Measured in wall time, so
# a fast first attempt (the normal case, ~1-3s) leaves the rest of the
# budget untouched and nothing changes for the common path.
#
# These are interactive-agent budgets, not batch ones. A user waiting on a
# document should never wait more than a coffee break, and a file that
# cannot be converted in 45s is a file to report, not to keep trying.
LIBREOFFICE_TOTAL_TIMEOUT = 45.0

DOC_SUFFIXES = {
    ".docx",
    ".docm",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".pptx",
    ".ppt",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".pdf",
    ".doc",
    ".epub",
    ".csv",
    ".tsv",
}


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated at {limit} characters]"


def _fail(path, reason) -> str:
    """A diagnostic, never an empty string. See note 3 in the module docstring."""
    return f"[could not read {path}: {reason}]"


def _soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------
# Native readers. These are fast and lossless for their own format.
# --------------------------------------------------------------------------


def _read_docx(path: Path) -> str:
    import docx  # python-docx

    document = docx.Document(str(path))
    parts: list[str] = []
    # Paragraphs and tables are separate streams in the document body and
    # iterating only one loses real content -- financial and legal .docx
    # files are mostly tables, and prose files often have none.
    for para in document.paragraphs:
        text = para.text.strip()
        if text:
            parts.append(text)
    for index, table in enumerate(document.tables, 1):
        parts.append(f"\n[table {index}]")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_xlsx(path: Path) -> str:
    import openpyxl

    # data_only=False keeps formulas, which is usually what a human wants to
    # see when they ask what a spreadsheet contains. A formula that reads
    # "=SUM(B2:B40)" is information; a formula rendered as its last cached
    # value throws that away and looks like a literal number.
    book = openpyxl.load_workbook(str(path), data_only=False, read_only=True)
    parts: list[str] = []
    try:
        for sheet in book.worksheets:
            parts.append(f"\n[Sheet: {sheet.title}]")
            for row in sheet.iter_rows():
                cells = []
                for cell in row:
                    if cell.value is None:
                        continue
                    cells.append(f"{cell.coordinate}={cell.value}")
                if cells:
                    parts.append("  ".join(cells))
    finally:
        book.close()
    return "\n".join(parts)


def _read_xls(path: Path) -> str:
    import xlrd

    book = xlrd.open_workbook(str(path))
    parts: list[str] = []
    for sheet in book.sheets():
        parts.append(f"\n[Sheet: {sheet.name}]")
        for r in range(sheet.nrows):
            cells = [
                str(sheet.cell_value(r, c))
                for c in range(sheet.ncols)
                if sheet.cell_value(r, c) not in (None, "")
            ]
            if cells:
                parts.append("  ".join(cells))
    return "\n".join(parts)


def _read_pdf(path: Path) -> str:
    binary = shutil.which("pdftotext")
    if not binary:
        return _fail(path, "pdftotext is not installed")
    result = subprocess.run(
        [binary, "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        return _fail(
            path, f"pdftotext exit {result.returncode}: {result.stderr.strip()[:200]}"
        )
    return result.stdout


def _read_catdoc(path: Path) -> str:
    binary = shutil.which("catdoc")
    if not binary:
        return _fail(path, "catdoc is not installed")
    result = subprocess.run(
        [binary, "-d", "utf-8", str(path)],
        capture_output=True,
        timeout=SUBPROCESS_TIMEOUT,
    )
    if result.returncode != 0:
        return _fail(path, f"catdoc exit {result.returncode}")
    return result.stdout.decode("utf-8", errors="replace")


def _read_delimited(path: Path) -> str:
    return path.read_text(errors="replace")


class _TagStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _read_markup(path: Path) -> str:
    # .epub is a zip of XHTML. Read the spine in order rather than every
    # member, or the book's metadata and nav page lead the output.
    if path.suffix.lower() == ".epub":
        try:
            with zipfile.ZipFile(path) as archive:
                names = [
                    n
                    for n in archive.namelist()
                    if n.lower().endswith((".xhtml", ".html"))
                ]
                names.sort(key=lambda n: ("nav" in n.lower(), n))
                out = []
                for name in names:
                    try:
                        out.append(
                            _read_markup_text(
                                archive.read(name).decode("utf-8", "replace")
                            )
                        )
                    except Exception:
                        continue
                joined = "\n\n".join(p for p in out if p)
                if joined.strip():
                    return joined
                return _fail(path, "epub contained no readable XHTML")
        except zipfile.BadZipFile:
            return _fail(path, "not a valid epub archive")
    return _read_markup_text(path.read_text(errors="replace"))


def _read_markup_text(markup: str) -> str:
    parser = _TagStripper()
    try:
        parser.feed(markup)
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    return parser.text()


# --------------------------------------------------------------------------
# LibreOffice fallback.
# --------------------------------------------------------------------------


def _soffice_convert(
    binary: str, profile: Path, target: Path, path: Path, to: str, timeout: float
) -> str:
    """One headless conversion. Returns stdout/stderr, never raises."""
    try:
        result = subprocess.run(
            [
                binary,
                f"-env:UserInstallation=file://{profile}",
                "--headless",
                "--norestore",
                "--invisible",
                "--nolockcheck",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                to,
                "--outdir",
                str(target),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout:.0f}s"
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return (result.stderr or result.stdout or "").strip()


def _first_output(target: Path) -> Path | None:
    """A non-empty converted file, or None.

    Existence is the success signal, not the exit code: a locked profile
    still exits 0 having written nothing, and an unsupported filter reports
    failure only in its stderr. See note 1 in the module docstring.
    """
    produced = [p for p in target.iterdir() if p.is_file() and p.stat().st_size > 0]
    return produced[0] if produced else None


def _read_via_libreoffice(path: Path) -> str:
    """Convert anything LibreOffice can open into plain text.

    Tries several export formats in turn, because no single one covers every
    document type. "Text (encoded)" is a *Writer* filter: handing it an
    Impress file makes the export fail with an Io/Write error while the
    conversion itself reports success, so a .pptx converts to nothing at
    all. PDF is the universal export and pdftotext then reads it, which
    covers slides, drawings, and anything else without a text filter.

    The isolated UserInstallation directory is the other half. It is
    prevention, not a workaround for an observed failure: see note 1 in the
    module docstring for what it does and does not buy.
    """
    binary = _soffice()
    if not binary:
        return _fail(path, "LibreOffice is not installed")

    # One deadline for the whole fallback, not one per format. A file that
    # stalls the first conversion must not get three full timeouts.
    deadline = time.monotonic() + LIBREOFFICE_TOTAL_TIMEOUT
    detail = "no export format was tried"

    for fmt in ("txt:Text (encoded):UTF8", "pdf", "html"):
        remaining = deadline - time.monotonic()
        if remaining <= 2.0:
            detail = (
                f"ran out of the {LIBREOFFICE_TOTAL_TIMEOUT:.0f}s conversion budget"
            )
            break
        with tempfile.TemporaryDirectory(prefix="master_ai_doc_") as work:
            profile = Path(work) / "profile"
            target = Path(work) / "out"
            target.mkdir()
            detail = _soffice_convert(
                binary,
                profile,
                target,
                path,
                fmt,
                timeout=min(SUBPROCESS_TIMEOUT, remaining),
            )
            produced = _first_output(target)
            if produced is None:
                continue
            try:
                if produced.suffix.lower() == ".pdf":
                    text = _read_pdf(produced)
                elif produced.suffix.lower() in (".html", ".htm"):
                    text = _read_markup_text(produced.read_text(errors="replace"))
                else:
                    text = produced.read_text(errors="replace")
            except OSError as e:
                detail = f"could not read converted output: {e}"
                continue
            # A converted file can still be empty of words, e.g. a deck of
            # pure images. Try the next format before declaring failure.
            if text.strip():
                return text.lstrip("﻿")
    return _fail(path, f"LibreOffice produced no readable text ({detail[:200]})")


# --------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------

_NATIVE = {
    ".docx": _read_docx,
    ".docm": _read_docx,
    ".xlsx": _read_xlsx,
    ".xlsm": _read_xlsx,
    ".xls": _read_xls,
    ".pdf": _read_pdf,
    ".doc": _read_catdoc,
    ".rtf": _read_catdoc,
    ".csv": _read_delimited,
    ".tsv": _read_delimited,
    ".epub": _read_markup,
    ".html": _read_markup,
    ".htm": _read_markup,
    ".xhtml": _read_markup,
}


def read_document(path, max_chars: int = MAX_CHARS) -> str:
    """Return the text of a document, or a diagnostic explaining the failure.

    Never raises and never returns "" -- an empty answer reads as "this
    document is blank", which is how a model ends up confidently reporting
    on a file it never read. See note 3 in the module docstring.
    """
    try:
        target = Path(path).expanduser()
    except Exception as e:
        return _fail(path, f"bad path: {e}")
    if not target.exists():
        return _fail(target, "no such file")
    if target.is_dir():
        return _fail(target, "is a directory")
    try:
        size = target.stat().st_size
    except OSError as e:
        return _fail(target, f"cannot stat: {e}")
    if size == 0:
        return f"[{target} is an empty file (0 bytes)]"
    if size > MAX_BYTES:
        return _fail(target, f"file is {size} bytes, over the {MAX_BYTES} limit")

    suffix = target.suffix.lower()

    def _done(raw: str) -> str:
        """Last gate. Garbage is a failure, not content -- see is_nonsense."""
        if raw.startswith("[") and (
            "could not read" in raw[:120] or "empty file" in raw
        ):
            return raw
        if is_nonsense(raw):
            return _fail(
                target,
                "extraction produced undecodable text; the file is probably "
                "corrupt or is not actually a document of that type",
            )
        return _clip(raw, max_chars)

    # Plain text first: no library, no subprocess, and it must never be
    # routed through a converter that could mangle it.
    if suffix in (".txt", ".md", ".log", ".json", ".yaml", ".yml", ".ini", ".py"):
        return _done(target.read_text(errors="replace"))

    reader = _NATIVE.get(suffix)
    if reader is not None:
        try:
            text = reader(target)
        except ImportError as e:
            text = _fail(target, f"missing library ({e}); trying LibreOffice")
        except Exception as e:
            text = _fail(target, f"{type(e).__name__}: {e}")
        # A native reader that failed still has a live fallback. Only
        # report the failure if the fallback adds nothing.
        if text.startswith("[") and "could not read" in text[:120]:
            fallback = _read_via_libreoffice(target)
            if not fallback.startswith("[") and not is_nonsense(fallback):
                text = fallback
        return _done(text)

    # Unlisted but document-shaped: attempt it rather than refuse. A refusal
    # is a worse answer than a failed attempt, because the user cannot tell
    # the difference and neither can the model.
    if suffix in DOC_SUFFIXES or not _looks_binary(target):
        return _done(_read_via_libreoffice(target))
    return _fail(target, f"unsupported file type {suffix or '(no extension)'}")


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return True
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    return printable / len(sample) < 0.90


# Codepoints that are essentially never legitimate in a document but are
# exactly what a mis-decoded byte stream produces. Script identity cannot
# do this job: CJK and Arabic characters are all isalnum() in Python, so
# mojibake made of kana and Arabic presentation forms passes any "is this
# real text" test built on alnum.
_CORRUPTION_RANGES = (
    (0xE000, 0xF8FF),  # private use area
    (0xF0000, 0xFFFFD),  # private use, supplementary planes
    (0x100000, 0x10FFFD),
    (0x0080, 0x009F),  # C1 controls
    (0xFDD0, 0xFDEF),  # noncharacters
)
_NONCHARS = frozenset({0xFFFE, 0xFFFF})


def _corruption_ratio(text: str) -> float:
    """Fraction of characters drawn from ranges that mean "bad decode"."""
    if not text:
        return 0.0
    bad = 0
    for ch in text:
        code = ord(ch)
        if code == 0xFFFD:  # the replacement character itself
            bad += 1
            continue
        if code in _NONCHARS:
            bad += 1
            continue
        for low, high in _CORRUPTION_RANGES:
            if low <= code <= high:
                bad += 1
                break
    return bad / len(text)


def is_nonsense(text: str, threshold: float = 0.005) -> bool:
    """True when extracted text is decoding garbage rather than content.

    A file of random bytes named .docx does not fail cleanly. LibreOffice
    will happily open it, decide it is a Writer document, and export the
    result -- so the reader returns a screen of mojibake that looks like a
    successful extraction. The model then answers questions about noise and
    is confidently wrong, which is the worst outcome this module can
    produce: worse than a refusal, because at the call site it is
    indistinguishable from success.

    Detection is by corruption marker, not by "does this look like
    language". Real documents -- including CJK, RTL, and accented ones --
    contain no private-use codepoints, C1 controls, or noncharacters at
    all, so a very small threshold separates garbage from genuine text
    without ever rejecting a legitimate language.
    """
    if not text.strip():
        return False  # genuinely empty is reported separately, not as garbage
    return _corruption_ratio(text) > threshold


def is_document(path) -> bool:
    """True when read_document should own this file rather than a text reader."""
    try:
        return Path(path).suffix.lower() in DOC_SUFFIXES
    except Exception:
        return False
