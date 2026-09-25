from __future__ import annotations

import html
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .schemas import ItemRecord

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml"}
ZIP_XML_SUFFIXES = {".docx", ".odt", ".ods", ".odp", ".pptx", ".xlsx"}


def _clean_text(raw: str, limit: int = 1200) -> str:
    text = html.unescape(raw)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _read_text(path: Path) -> str:
    return _clean_text(path.read_text(encoding="utf-8", errors="ignore"))


def _xml_text_from_zip(path: Path) -> str:
    names: list[str]
    suffix = path.suffix.lower()
    if suffix == ".docx":
        names = ["word/document.xml"]
    elif suffix in {".odt", ".ods", ".odp"}:
        names = ["content.xml"]
    elif suffix == ".pptx":
        names = []
        with zipfile.ZipFile(path) as zf:
            names = sorted(
                n
                for n in zf.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
    elif suffix == ".xlsx":
        names = ["xl/sharedStrings.xml"]
    else:
        names = []

    chunks: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in names[:20]:
            try:
                data = zf.read(name)
            except KeyError:
                continue
            try:
                root = ElementTree.fromstring(data)
            except ElementTree.ParseError:
                continue
            for elem in root.iter():
                if elem.text:
                    chunks.append(elem.text)
    return _clean_text(" ".join(chunks))


def preview_for_path(path: Path) -> dict:
    suffix = path.suffix.lower()
    record = {
        "path": str(path),
        "name": path.name,
        "suffix": suffix,
        "preview_type": "open_file",
        "text": "",
        "error": "",
    }
    try:
        if suffix in TEXT_SUFFIXES:
            record["preview_type"] = "text_excerpt"
            record["text"] = _read_text(path)
        elif suffix in ZIP_XML_SUFFIXES:
            record["preview_type"] = "document_text_excerpt"
            record["text"] = _xml_text_from_zip(path)
        elif suffix in {
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".heic",
            ".bmp",
            ".tiff",
        }:
            record["preview_type"] = "image_file"
        elif suffix in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
            record["preview_type"] = "video_file"
        elif suffix == ".pdf":
            record["preview_type"] = "pdf_file"
    except Exception as exc:
        record["error"] = str(exc)
    return record


def build_previews(
    items: list[ItemRecord], *, include_content: bool, limit: int = 100
) -> list[dict]:
    previews: list[dict] = []
    for item in items[:limit]:
        path = Path(item.identity.get("path", ""))
        record = {
            "item_id": item.item_id,
            "display_name": item.display_name,
            "path": str(path),
            "mime": item.mime,
            "size_bytes": item.size_bytes,
            "category_guess": item.category_guess,
            "sensitivity": item.sensitivity,
            "preview": {},
        }
        if include_content and path.exists() and path.is_file():
            record["preview"] = preview_for_path(path)
        previews.append(record)
    return previews


def write_preview_files(
    run_dir: Path, items: list[ItemRecord], *, include_content: bool
) -> None:
    previews = build_previews(items, include_content=include_content)
    preview_json = run_dir / "previews.json"
    preview_md = run_dir / "reports" / "previews.md"
    preview_json.write_text(
        json.dumps(previews, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# Sensei File Preview Index",
        "",
        f"- Items shown: {len(previews)}",
        f"- Content excerpts: {'included' if include_content else 'metadata only'}",
        "",
    ]
    for idx, record in enumerate(previews, start=1):
        lines.append(f"## {idx}. {record['display_name']}")
        lines.append(f"- Path: `{record['path']}`")
        lines.append(
            f"- Type: {record['mime']} | Category: {record['category_guess']} | Sensitivity: {record['sensitivity']}"
        )
        preview = record.get("preview") or {}
        if preview.get("preview_type"):
            lines.append(f"- Preview: {preview.get('preview_type')}")
        if preview.get("text"):
            lines.append("")
            lines.append("```text")
            lines.append(str(preview["text"]))
            lines.append("```")
        if preview.get("error"):
            lines.append(f"- Preview error: {preview['error']}")
        lines.append("")
    preview_md.write_text("\n".join(lines), encoding="utf-8")
