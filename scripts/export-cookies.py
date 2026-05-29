#!/usr/bin/env python3
"""Export YouTube cookies from Firefox to Netscape format (cookies.txt)."""
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROFILE_GLOB = os.path.expanduser("~/.mozilla/firefox/*/cookies.sqlite")
OUTPUT = Path("cookies.txt")


def find_cookies_db() -> Path:
    matches = sorted(glob.glob(PROFILE_GLOB))
    if not matches:
        sys.exit("No Firefox profile found at ~/.mozilla/firefox/*/cookies.sqlite")
    for m in matches:
        if "default-release" in m:
            return Path(m)
    return Path(matches[0])


def main():
    db = find_cookies_db()
    print(f"Reading from: {db}")

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        shutil.copy2(db, tmp.name)
        tmp_path = tmp.name

    try:
        conn = sqlite3.connect(tmp_path)
        rows = conn.execute(
            "SELECT host, path, isSecure, expiry, name, value "
            "FROM moz_cookies WHERE host LIKE '%youtube.com%'"
        ).fetchall()
        conn.close()
    finally:
        os.unlink(tmp_path)

    if not rows:
        sys.exit(
            "No YouTube cookies found — sign in to music.youtube.com in Firefox first"
        )

    with open(OUTPUT, "w") as f:
        f.write("# Netscape HTTP Cookie File\n\n")
        for host, path, secure, expiry, name, value in rows:
            subdomain = "TRUE" if host.startswith(".") else "FALSE"
            f.write(
                f"{host}\t{subdomain}\t{path}\t{'TRUE' if secure else 'FALSE'}\t{expiry}\t{name}\t{value}\n"
            )

    print(f"Saved {len(rows)} YouTube cookie(s) to {OUTPUT}")


if __name__ == "__main__":
    main()
