#!/usr/bin/env python3
"""
gmail_init.py — Bootstrap ~/.gmail_imap.gpg from Thunderbird's stored token.

Reads the Gmail OAuth2 refresh token directly from Thunderbird's NSS database
(no re-authorization required). Saves it encrypted to ~/.gmail_imap.gpg in the
same format gmail_mail.py expects (user:refresh_token).

Usage:
  python3 ~/scripts/gmail_init.py               # auto-detect Thunderbird profile
  python3 ~/scripts/gmail_init.py --profile /path/to/thunderbird/profile

Requires: libnss3.so (already installed with Thunderbird)
"""

import argparse
import base64
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

CREDS_FILE = os.path.expanduser("~/.gmail_imap.gpg")
PASSPHRASE_CMD = f"echo '{os.uname().nodename}-gmail-imap-key'"
THUNDERBIRD_PROFILE = os.path.expanduser("~/.thunderbird/47nuc58i.default-release")


# NSS data structures
class SECItem(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ("len", ctypes.c_uint),
    ]


def load_nss(profile_dir: str):
    nss3 = ctypes.CDLL("libnss3.so")
    nss3.NSS_Init.restype = ctypes.c_int
    nss3.NSS_Shutdown.restype = ctypes.c_int
    nss3.PK11SDR_Decrypt.restype = ctypes.c_int
    nss3.SECITEM_ZfreeItem.restype = None
    rc = nss3.NSS_Init(profile_dir.encode())
    if rc != 0:
        raise RuntimeError(f"NSS_Init failed (rc={rc}). Wrong profile directory?")
    return nss3


def nss_decrypt(nss3, b64_ciphertext: str) -> str:
    enc = base64.b64decode(b64_ciphertext)
    enc_buf = (ctypes.c_ubyte * len(enc))(*enc)

    inp = SECItem()
    inp.type = 0
    inp.data = ctypes.cast(enc_buf, ctypes.POINTER(ctypes.c_ubyte))
    inp.len = len(enc)

    out = SECItem()
    out.type = 0
    out.data = None
    out.len = 0

    rc = nss3.PK11SDR_Decrypt(ctypes.byref(inp), ctypes.byref(out), None)
    if rc != 0:
        raise RuntimeError("PK11SDR_Decrypt failed — master password set?")

    result = bytes(
        ctypes.cast(out.data, ctypes.POINTER(ctypes.c_ubyte * out.len)).contents
    ).decode()
    nss3.SECITEM_ZfreeItem(ctypes.byref(out), False)
    return result


def find_gmail_creds(profile_dir: str):
    logins_path = Path(profile_dir) / "logins.json"
    if not logins_path.exists():
        raise FileNotFoundError(f"logins.json not found in {profile_dir}")

    with open(logins_path) as f:
        data = json.load(f)

    nss3 = load_nss(profile_dir)

    email = None
    refresh_token = None

    for entry in data.get("logins", []):
        host = entry.get("hostname", "")
        # Look for the Google OAuth2 token entry
        if "accounts.google.com" not in host and "gmail" not in host.lower():
            continue
        if "oauth" not in host.lower() and "imap" not in host.lower():
            continue

        try:
            user = nss_decrypt(nss3, entry["encryptedUsername"])
            pwd = nss_decrypt(nss3, entry["encryptedPassword"])
        except Exception as e:
            print(f"  skip {host}: {e}")
            continue

        # The oauth://accounts.google.com entry has email as user, refresh token as pwd
        if "oauth" in host.lower() and "@" in user:
            email = user
            refresh_token = pwd
            print(f"  Found OAuth token for: {email} (from {host})")
            break

    nss3.NSS_Shutdown()

    if not email or not refresh_token:
        raise RuntimeError(
            "Gmail OAuth token not found in Thunderbird profile.\n"
            "Make sure Thunderbird has Gmail configured and you've signed in."
        )

    return email, refresh_token


def save_creds(email: str, refresh_token: str):
    passphrase = subprocess.check_output(PASSPHRASE_CMD, shell=True).decode().strip()
    data = f"{email}:{refresh_token}"
    proc = subprocess.Popen(
        [
            "gpg",
            "--batch",
            "--yes",
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "--passphrase",
            passphrase,
            "-o",
            CREDS_FILE,
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc.communicate(data.encode())
    os.chmod(CREDS_FILE, 0o600)
    print(f"  Saved to {CREDS_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap Gmail OAuth2 credentials from Thunderbird"
    )
    parser.add_argument(
        "--profile",
        default=THUNDERBIRD_PROFILE,
        help=f"Thunderbird profile dir (default: {THUNDERBIRD_PROFILE})",
    )
    args = parser.parse_args()

    print(f"Reading Thunderbird profile: {args.profile}")

    try:
        email, token = find_gmail_creds(args.profile)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    save_creds(email, token)
    print("\nDone. Run: python3 ~/scripts/gmail_mail.py --unread")


if __name__ == "__main__":
    main()
