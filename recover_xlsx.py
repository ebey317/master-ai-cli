#!/usr/bin/env python3
"""
recover_xlsx.py — Carve deleted XLSX/ODS files from a raw ext4 device.

Strategy: A ZIP (xlsx/ods) stores its member filenames UNCOMPRESSED on disk.
We scan the raw device for those plaintext anchors (e.g. 'xl/workbook.xml',
'content.xml'), find the enclosing ZIP boundaries (PK\\x03\\x04 ... PK\\x05\\x06),
carve each candidate, and validate it as a real spreadsheet containing our
known sheet/business names.

MUST run as root:  sudo python3 ~/scripts/recover_xlsx.py
Recovered files land in ~/recovered/
"""

import io
import os
import pathlib
import re
import sys
import zipfile

DEVICE = "/dev/nvme0n1p5"
OUT = pathlib.Path.home() / "recovered"
# If run under sudo, HOME may be /root — force the real user's home
if os.environ.get("SUDO_USER"):
    OUT = pathlib.Path(f"/home/{os.environ['SUDO_USER']}") / "recovered"

# Plaintext anchors that appear inside the target ZIPs
ANCHORS = [b"xl/workbook.xml", b"content.xml", b"xl/sharedStrings.xml"]
# Known strings that prove it's OUR file (any one match = keep)
SIGNATURES = [
    b"Fair Chance",
    b"BioVega",
    b"Sunkissed",
    b"Care Link",
    b"MCP School",
    b"Life Party",
    b"Future Computer",
    b"Drive-In",
]

ZIP_LOCAL = b"PK\x03\x04"
ZIP_EOCD = b"PK\x05\x06"

CHUNK = 64 * 1024 * 1024  # 64 MB read window
OVERLAP = 1 * 1024 * 1024  # 1 MB overlap so anchors aren't split
MAX_ZIP = 5 * 1024 * 1024  # carve up to 5 MB around an anchor


def carve_at(f, anchor_abs):
    """Given an absolute device offset inside a zip, find its bounds and carve."""
    lo = max(0, anchor_abs - MAX_ZIP)
    f.seek(lo)
    window = f.read(MAX_ZIP * 2)
    rel = anchor_abs - lo
    # nearest local-file-header signature at/just before the anchor
    start = window.rfind(ZIP_LOCAL, 0, rel)
    if start < 0:
        return None
    # EOCD after the anchor; carve to just past its 22-byte record (+comment)
    eocd = window.find(ZIP_EOCD, rel)
    if eocd < 0:
        return None
    end = eocd + 22
    if end <= len(window) - 2:
        comment_len = int.from_bytes(window[eocd + 20 : eocd + 22], "little")
        end += comment_len
    return window[start:end]


def validate(blob):
    """Return (ok, info) if blob is a readable spreadsheet matching our file."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        names = z.namelist()
        if not any("workbook.xml" in n or "content.xml" in n for n in names):
            return False, None
        text = b""
        for n in names:
            if n.endswith(("sharedStrings.xml", "content.xml")) or "sheet" in n:
                try:
                    text += z.read(n)
                except Exception:
                    pass
        hits = [s.decode() for s in SIGNATURES if s in text or s in blob]
        return (len(hits) > 0), {
            "members": len(names),
            "hits": hits,
            "bytes": len(blob),
        }
    except Exception:
        return False, None


def main():
    if not os.access(DEVICE, os.R_OK):
        print(f"ERROR: cannot read {DEVICE}. Run with sudo.")
        sys.exit(1)
    OUT.mkdir(exist_ok=True)
    size = os.path.getsize(DEVICE) if os.path.exists(DEVICE) else 0
    try:
        size = os.lseek(os.open(DEVICE, os.O_RDONLY), 0, os.SEEK_END)
    except Exception:
        pass

    print(f"Scanning {DEVICE} ({size / 1e9:.0f} GB) for deleted spreadsheets...")
    found = 0
    seen_offsets = set()
    pat = re.compile(b"|".join(re.escape(a) for a in ANCHORS))

    with open(DEVICE, "rb") as f:
        pos = 0
        while pos < size:
            f.seek(pos)
            buf = f.read(CHUNK)
            if not buf:
                break
            for m in pat.finditer(buf):
                anchor_abs = pos + m.start()
                bucket = anchor_abs // MAX_ZIP
                if bucket in seen_offsets:
                    continue
                seen_offsets.add(bucket)
                blob = carve_at(f, anchor_abs)
                if not blob:
                    continue
                ok, info = validate(blob)
                if ok:
                    found += 1
                    ext = "xlsx" if b"xl/workbook.xml" in blob else "ods"
                    dest = OUT / f"recovered_{found:02d}_{anchor_abs}.{ext}"
                    dest.write_bytes(blob)
                    if os.environ.get("SUDO_UID"):
                        os.chown(
                            dest,
                            int(os.environ["SUDO_UID"]),
                            int(os.environ.get("SUDO_GID", 0)),
                        )
                    print(
                        f"  ✓ {dest.name}  members={info['members']} "
                        f"size={info['bytes']}  hits={info['hits']}"
                    )
            pos += CHUNK - OVERLAP
            print(f"    ...{pos / size * 100:.0f}%", end="\r", flush=True)

    print(f"\nDone. {found} candidate(s) in {OUT}")
    if os.environ.get("SUDO_UID"):
        os.chown(OUT, int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", 0)))


if __name__ == "__main__":
    main()
