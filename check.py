#!/usr/bin/env python3
"""
Check-Modus, Sammel-Datei-Variante.

Zwei Pfade:
  - CurseForge  -> offizielle API, liefert Version direkt
  - alles andere-> HTML holen, Textbereich extrahieren, in Sammel-Datei

Schreibt NICHTS in den Mods-Ordner. Laedt keine Mod-Dateien herunter.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).parent
REGISTRY = ROOT / "registry.json"
CACHE = ROOT / "cache"
LOGS = ROOT / "logs"

load_dotenv(ROOT / ".env")
CF_KEY = os.getenv("CURSEFORGE_API_KEY")
CF_API = "https://api.curseforge.com"
CF_GAME_SIMS4 = 432

DELAY = 2.0
CACHE_TTL = 6 * 3600
TIMEOUT = 20
MAX_CHARS = 3000

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Session fuer HTML-Abrufe (Browser-Header, Cookies bleiben erhalten)
SESSION = requests.Session()
SESSION.headers.update(BROWSER_HEADERS)

# Eigene Session fuer die CurseForge-API — erbt bewusst KEINE Browser-Header,
# sonst kollidiert Accept: text/html mit der JSON-API und es gibt 403.
CF_SESSION = requests.Session()
CF_SESSION.headers.clear()
CF_SESSION.headers.update({
    "x-api-key": CF_KEY or "",
    "Accept": "application/json",
    "User-Agent": "sims4-updater/0.1",
})

# CurseForge releaseType: 1=release, 2=beta, 3=alpha
RELEASE_TYPE = {1: "release", 2: "beta", 3: "alpha"}


# ---------------------------------------------------------------- CurseForge

def is_curseforge(url):
    return bool(url) and "curseforge.com" in url


def cf_slug(url):
    m = re.search(r"curseforge\.com/sims4/[^/]+/([^/?#]+)", url)
    return m.group(1) if m else None


def cf_resolve_id(url):
    """Projekt-ID ueber die Suche aufloesen. Fallback, wenn nicht in Registry."""
    slug = cf_slug(url)
    if not slug:
        return None, "kein slug in URL"
    try:
        r = CF_SESSION.get(
            f"{CF_API}/v1/mods/search",
            params={"gameId": CF_GAME_SIMS4, "slug": slug},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.status_code != 200:
        return None, f"suche HTTP {r.status_code}"
    hits = r.json().get("data", [])
    if not hits:
        return None, f"slug '{slug}' nicht gefunden"
    return hits[0]["id"], None


def cf_latest(mod_id):
    """Neueste Datei zurueckgeben. Bevorzugt releaseType 1, sonst neueste ueberhaupt."""
    try:
        r = CF_SESSION.get(
            f"{CF_API}/v1/mods/{mod_id}/files",
            params={"pageSize": 20},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if r.status_code == 403:
        return None, "HTTP 403 — API-Key pruefen"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"

    files = r.json().get("data", [])
    if not files:
        return None, "keine Dateien"

    files.sort(key=lambda f: f.get("fileDate", ""), reverse=True)
    stable = [f for f in files if f.get("releaseType") == 1]
    pick = stable[0] if stable else files[0]

    return {
        "display_name": pick.get("displayName", ""),
        "file_name": pick.get("fileName", ""),
        "date": (pick.get("fileDate") or "")[:10],
        "release_type": RELEASE_TYPE.get(pick.get("releaseType"), "?"),
        "game_versions": pick.get("gameVersions", []),
        "id": pick.get("id"),
    }, None


# ---------------------------------------------------------------------- HTML

def cache_path(url):
    return CACHE / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".html")


def fetch_html(url):
    cf = cache_path(url)
    if cf.exists() and (time.time() - cf.stat().st_mtime) < CACHE_TTL:
        return cf.read_text(encoding="utf-8", errors="replace"), "cache"
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return None, f"fehler: {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return None, f"fehler: HTTP {resp.status_code}"
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return None, f"fehler: kein HTML ({ctype})"
    cf.write_text(resp.text, encoding="utf-8")
    time.sleep(DELAY)
    return resp.text, "web"


def extract(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    pat = re.compile(
        r"(v\s?\d+[\w.\-]*|version|update|release|\d{1,2}\.\d{1,2}\.\d{2,4}"
        r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2},?\s+\d{4})",
        re.I)
    hits = [ln for ln in text.splitlines() if pat.search(ln)][:40]
    return title, hits, text[:MAX_CHARS]


# ---------------------------------------------------------------------- main

def main():
    if not CF_KEY:
        print("Warnung: CURSEFORGE_API_KEY fehlt, CurseForge-Eintraege werden "
              "uebersprungen.", file=sys.stderr)

    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    mods = data["mods"]
    CACHE.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)

    out = LOGS / f"collect_{date.today().isoformat()}.md"
    resolved = []          # CurseForge, Version schon bekannt
    stats = {"cf": 0, "html": 0, "unknown": 0, "fehler": 0}

    with out.open("w", encoding="utf-8") as fh:
        fh.write(f"# Sammel-Datei {datetime.now():%Y-%m-%d %H:%M}\n\n")
        fh.write(f"Registry-Version {data.get('registry_version')}, "
                 f"{len(mods)} Eintraege.\n\n")
        fh.write("CurseForge-Eintraege sind bereits aufgeloest (Abschnitt 1) "
                 "und brauchen keine Auswertung.\n"
                 "Aufgabe fuer Abschnitt 2: aktuellste Version bestimmen und "
                 "mit `installed` vergleichen.\n\n")

        # --- Durchgang 1: CurseForge
        fh.write("---\n\n# 1. CurseForge (automatisch aufgeloest)\n\n")
        for mod in mods:
            src = mod.get("source", "")
            if not is_curseforge(src):
                continue
            name = mod.get("name", "?")
            print(f"[cf] {name}", file=sys.stderr)

            mod_id = mod.get("curseforge_id")
            note = ""
            if not mod_id:
                mod_id, err = cf_resolve_id(src)
                if not mod_id:
                    fh.write(f"## {name}\n\n- **Fehler:** ID nicht aufloesbar "
                             f"({err})\n- source: {src}\n\n")
                    stats["fehler"] += 1
                    continue
                note = " (per Suche aufgeloest — bitte in Registry eintragen)"

            latest, err = cf_latest(mod_id)
            time.sleep(0.3)
            if not latest:
                fh.write(f"## {name}\n\n- **Fehler:** {err}\n"
                         f"- curseforge_id: {mod_id}\n\n")
                stats["fehler"] += 1
                continue
            
            gv = ", ".join(latest["game_versions"]) or "—"
            inst = mod.get("installed_version", "") or "—"
            fh.write(f"## {name}{note}\n\n")
            fh.write(f"- installed: `{inst}`\n")
            fh.write(f"- neueste: `{latest['display_name']}`\n")
            fh.write(f"- datei: `{latest['file_name']}`\n")
            fh.write(f"- datum: {latest['date']} ({latest['release_type']})\n")
            fh.write(f"- zuletzt aktualisiert mit: {gv}\n")
            fh.write(f"- curseforge_id: {mod_id}\n\n")
            resolved.append({"name": name,
                             "installed": mod.get("installed_version", ""),
                             "latest": latest["display_name"],
                             "date": latest["date"]})
            stats["cf"] += 1

        # --- Durchgang 2: HTML
        fh.write("---\n\n# 2. Sonstige Quellen (auszuwerten)\n\n")
        for mod in mods:
            src = mod.get("source", "unknown")
            if is_curseforge(src):
                continue
            name = mod.get("name", "?")
            inst = mod.get("installed_version", "") or "—"
            print(f"[html] {name}", file=sys.stderr)

            fh.write(f"## {name}\n\n- installed: `{inst}`\n- source: {src}\n")
            if not src or src == "unknown":
                fh.write("- status: **keine Quelle hinterlegt**\n\n---\n\n")
                stats["unknown"] += 1
                continue

            html, origin = fetch_html(src)
            if html is None:
                fh.write(f"- status: **{origin}**\n\n---\n\n")
                stats["fehler"] += 1
                continue

            title, hits, body = extract(html)
            fh.write(f"- status: ok ({origin})\n- title: {title}\n\n")
            if hits:
                fh.write("### Zeilen mit Versionsmuster\n\n```\n")
                fh.write("\n".join(hits))
                fh.write("\n```\n\n")
            fh.write("### Seitentext (gekuerzt)\n\n```\n")
            fh.write(body)
            fh.write("\n```\n\n---\n\n")
            stats["html"] += 1

    # Kurzuebersicht der CurseForge-Treffer
    if resolved:
        print("\nCurseForge:", file=sys.stderr)
        for r in resolved:
            flag = "" if r["installed"] and r["installed"] in r["latest"] else "  <-- pruefen"
            print(f"  {r['name']:<35} {r['installed'] or '—':<12} "
                  f"-> {r['latest']}{flag}", file=sys.stderr)

    print(f"\n{stats['cf']} via API, {stats['html']} via HTML, "
          f"{stats['unknown']} ohne Quelle, {stats['fehler']} Fehler",
          file=sys.stderr)
    print(f"Datei: {out} ({out.stat().st_size / 1024:.0f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()