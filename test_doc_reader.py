"""Tests for document reading.

The bug these exist to prevent is not a crash. It is silence. A reader that
returns "" or returns garbage looks exactly like a reader that worked, so
the failure surfaces as a confident, wrong answer from the model rather
than as an error. Most of what follows is about distinguishing those cases.

LibreOffice tests are skipped when soffice is absent, because it is a heavy
optional dependency and its absence must not fail the suite.
"""

from __future__ import annotations

import os
import shutil
import zipfile

import pytest

from doc_reader import (
    _corruption_ratio,
    _looks_binary,
    is_document,
    is_nonsense,
    read_document,
)

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
needs_soffice = pytest.mark.skipif(not SOFFICE, reason="LibreOffice not installed")


# --------------------------------------------------------------------------
# Fixtures: real files, not mocks. A mock of python-docx proves nothing
# about whether python-docx can parse a real .docx.
# --------------------------------------------------------------------------


@pytest.fixture
def docx_file(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Quarterly Report", 0)
    d.add_paragraph("Revenue grew 12 percent to 4.1 million dollars.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Region"
    table.cell(0, 1).text = "Revenue"
    table.cell(1, 0).text = "West"
    table.cell(1, 1).text = "2.4M"
    path = tmp_path / "report.docx"
    d.save(path)
    return path


@pytest.fixture
def xlsx_file(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Sales"
    sheet["A1"] = "Region"
    sheet["B1"] = "Revenue"
    sheet["A2"] = "West"
    sheet["B2"] = 2400000
    sheet["A3"] = "Total"
    sheet["B3"] = "=SUM(B2:B2)"
    path = tmp_path / "sales.xlsx"
    book.save(path)
    return path


# --------------------------------------------------------------------------
# Native readers.
# --------------------------------------------------------------------------


def test_docx_returns_prose_and_tables(docx_file):
    text = read_document(docx_file)
    assert "Quarterly Report" in text
    assert "4.1 million dollars" in text
    # A reader that only walks paragraphs silently loses every table, and
    # tables are most of a spreadsheet-shaped or legal document.
    assert "2.4M" in text
    assert "Region" in text


def test_xlsx_returns_cells_with_sheet_name_and_formulas(xlsx_file):
    text = read_document(xlsx_file)
    assert "Sales" in text
    assert "2400000" in text
    # Formulas are the document. Rendering the cached value instead makes
    # a formula look like a hardcoded number.
    assert "=SUM(B2:B2)" in text


def test_plain_text_is_not_routed_through_a_converter(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# Title\n\nSome notes with unicode: café, 東京, 25%.\n")
    text = read_document(path)
    assert "café" in text and "東京" in text


# --------------------------------------------------------------------------
# Failures must be loud.
# --------------------------------------------------------------------------


def test_missing_file_says_so(tmp_path):
    out = read_document(tmp_path / "nope.docx")
    assert "no such file" in out


def test_empty_file_is_reported_not_silently_blank(tmp_path):
    path = tmp_path / "empty.docx"
    path.write_bytes(b"")
    out = read_document(path)
    assert "empty file" in out
    assert out.strip(), "an empty document must still say something"


def test_directory_is_rejected(tmp_path):
    d = tmp_path / "thing.docx"
    d.mkdir()
    assert "is a directory" in read_document(d)


def test_unknown_binary_gets_a_clear_refusal(tmp_path):
    path = tmp_path / "mystery.bin"
    path.write_bytes(b"\x00\x01\x02\x03" * 500)
    out = read_document(path)
    assert "unsupported file type" in out


def test_no_reader_ever_returns_silence(tmp_path):
    """The invariant that matters most, across every failure shape."""
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"")
    mystery = tmp_path / "mystery.bin"
    mystery.write_bytes(bytes(range(256)) * 20)
    for path in (bad, mystery, tmp_path / "absent.pdf", tmp_path / "d.docx"):
        if path.is_dir():
            path.mkdir(exist_ok=True)
        assert read_document(path).strip(), f"{path} produced silent empty output"


@needs_soffice
def test_corrupt_document_is_rejected_not_converted_to_mojibake(tmp_path):
    """The regression that shaped this module.

    Random bytes named .docx do not fail cleanly: LibreOffice opens them,
    decides they are a Writer document, and exports mojibake. A reader that
    returns that is indistinguishable from success at the call site, so the
    model answers questions about noise and is confidently wrong.
    """
    path = tmp_path / "corrupt.docx"
    path.write_bytes(bytes(range(256)) * 8)
    out = read_document(path)
    assert "could not read" in out
    assert _corruption_ratio(out) == 0.0, "diagnostic must itself be clean"


# --------------------------------------------------------------------------
# Garbage detection.
# --------------------------------------------------------------------------


def test_mojibake_is_detected():
    assert is_nonsense("ﻂπ㭣豐ﳺ셽襸亨")


@pytest.mark.parametrize(
    "text",
    [
        "The quick brown fox jumped over 42 lazy dogs.",
        "東京は日本の首都です，人口は約千四百万人。",  # CJK
        "مرحبا بالعالم، هذه تجربة للقراءة.",  # RTL
        "café naïve — résumé, jalapeño, Ångström.",  # accented
        "Quarterly result: +12% (see fig. 3)",  # punctuation
        "status ✅ shipped 🚀",  # emoji
    ],
)
def test_real_text_is_never_flagged_as_garbage(text):
    """A garbage filter that rejects a language is a bug, not caution.

    Script identity cannot be the signal: CJK and Arabic characters are all
    isalnum() in Python, so a mojibake made of kana and Arabic presentation
    forms defeats any alnum-based test. The signal is private-use and
    control codepoints, which real documents never contain.
    """
    assert not is_nonsense(text)
    assert _corruption_ratio(text) == 0.0


def test_empty_string_is_not_garbage():
    assert not is_nonsense("")
    assert not is_nonsense("   ")


def test_corruption_ratio_is_proportional():
    assert _corruption_ratio("") == 0.0
    assert _corruption_ratio("a" * 100) == 0.0
    assert _corruption_ratio("a" * 90 + "﷐﷑﷒") > 0.02


# --------------------------------------------------------------------------
# Routing and limits.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.docx", True),
        ("a.xlsx", True),
        ("a.pdf", True),
        ("a.pptx", True),
        ("a.odt", True),
        ("a.txt", False),
        ("a.py", False),
        ("a", False),
    ],
)
def test_is_document_gates_only_real_document_types(name, expected):
    assert is_document(name) is expected


def test_output_is_clipped_with_a_visible_marker(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("x" * 5000)
    out = read_document(path, max_chars=1000)
    assert len(out) < 1200
    assert "truncated" in out


def test_zip_based_docx_but_not_a_real_archive_is_reported(tmp_path):
    path = tmp_path / "fake.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "not a word document")
    out = read_document(path)
    assert "could not read" in out


def test_looks_binary_flags_nul_and_passes_text(tmp_path):
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"abc\x00def")
    text = tmp_path / "t.txt"
    text.write_text("plain readable text")
    assert _looks_binary(binary)
    assert not _looks_binary(text)


@needs_soffice
def test_libreoffice_fallback_reads_a_format_with_no_native_reader(tmp_path):
    """A .pptx has no native reader here, so it must go through LibreOffice.

    This is the real shape of the fallback: a valid document in a format
    the module has no library for. The normal case costs 1-3s.
    """
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Board Deck"
    path = tmp_path / "deck.pptx"
    deck.save(path)

    out = read_document(path)
    assert "Board Deck" in out, out[:200]


@needs_soffice
def test_pathological_input_fails_within_the_budget(tmp_path):
    """A file that stalls the converter must return, not hang.

    The per-attempt timeout is not a bound on its own: three export formats
    at a full timeout each would leave the agent apparently hung on one
    document. The whole fallback shares one deadline, so the worst case is
    bounded no matter how many formats are attempted.
    """
    import time as _time

    from doc_reader import LIBREOFFICE_TOTAL_TIMEOUT

    junk = tmp_path / "junk.odp"
    junk.write_bytes(os.urandom(64 * 1024))

    started = _time.monotonic()
    out = read_document(junk)
    elapsed = _time.monotonic() - started

    assert out.strip(), "must report, never return silence"
    assert elapsed < LIBREOFFICE_TOTAL_TIMEOUT + 15, (
        f"fallback took {elapsed:.0f}s, over its {LIBREOFFICE_TOTAL_TIMEOUT:.0f}s budget"
    )
