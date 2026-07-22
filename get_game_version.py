#!/usr/bin/env python3
"""Liest die installierte Sims-4-Spielversion aus GameVersion.txt."""

import re
import sys
from pathlib import Path

# Basis-Pfad der Installation (Mac App-Paket)
DEFAULT_BASE = "/Users/dominique/Documents/Electronic Arts/The Sims 4"

VERSION_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")


def find_version_file(base: Path) -> Path | None:
    """Sucht GameVersion.txt unterhalb des Basis-Pfads."""
    # Häufigster Ort zuerst
    direct = base / "GameVersion.txt"
    if direct.is_file():
        return direct
    # Fallback: rekursiv suchen
    for match in base.rglob("GameVersion.txt"):
        return match
    return None


def read_game_version(path: Path) -> str | None:
    """Liest die Version robust aus der Datei (UTF-16 mit BOM / führende Bytes)."""
    raw = path.read_bytes()
    # UTF-16 zuerst versuchen, dann UTF-8 als Fallback
    for encoding in ("utf-16", "utf-8"):
        try:
            text = raw.decode(encoding, errors="ignore")
        except Exception:
            continue
        m = VERSION_RE.search(text)
        if m:
            return m.group(0)
    return None


def get_game_version(base: str = DEFAULT_BASE) -> str | None:
    base_path = Path(base)
    if not base_path.exists():
        print(f"Pfad nicht gefunden: {base_path}", file=sys.stderr)
        return None

    version_file = find_version_file(base_path)
    if version_file is None:
        print(f"GameVersion.txt nicht gefunden unter: {base_path}", file=sys.stderr)
        return None

    version = read_game_version(version_file)
    if version is None:
        print(f"Keine Version aus {version_file} lesbar.", file=sys.stderr)
    return version


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    version = get_game_version(base)
    if version:
        print(version)
        sys.exit(0)
    sys.exit(1)
