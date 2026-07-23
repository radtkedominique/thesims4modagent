#!/usr/bin/env python3
"""
evaluate.py — wertet die Collect-Datei (logs/collect_<date>.md) aus.

Zwei Pfade pro Mod-Block:
  - Block hat bereits eine Version (CurseForge via API) -> direkt vergleichen, kein LLM
  - Block hat nur gescrapten Seitentext -> gemma3:4b extrahiert die Versionsnummer

Das LLM entscheidet NICHT, ob etwas neuer ist. Es extrahiert nur Strings.
Der Vergleich passiert deterministisch in compare().

Aufruf:
  uv run evaluate.py                    # neueste logs/collect_*.md, Game-Version automatisch
  uv run evaluate.py --debug            # zeigt erkannte Felder je Block
  uv run evaluate.py --out logs/result.json
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from packaging.version import Version, InvalidVersion
except ImportError:
    sys.exit("packaging fehlt -> uv add packaging")

try:
    import ollama
except ImportError:
    sys.exit("ollama fehlt -> uv add ollama")


MODEL_DEFAULT = "gemma3:4b"

SYSTEM_PROMPT = (
    "Du bekommst rohen, gescrapten Text von einer Mod-Downloadseite.\n"
    "Aufgabe: finde die NEUESTE zum Download angebotene Versionsnummer des Mods.\n\n"
    "Regeln:\n"
    "- Gib die Versionsnummer VOLLSTAENDIG an. Kuerze niemals ab: "
    "'4.2' bleibt '4.2', nicht '4'.\n"
    "- Erfinde nichts. Die Version muss woertlich im Text vorkommen.\n"
    "- Nimm nicht die installierte oder eine aeltere Version.\n"
    "- Verwechsle Mod-Version und Spielversion nicht. Spielversionen beginnen "
    "mit '1.' und haben 3-4 Teile (z.B. 1.124.63). Diese gehoert in "
    "compatible_game_version, nicht in latest_version.\n"
    "- evidence: die Textzeile, in der du die Version gefunden hast, woertlich.\n"
    "- Findest du keine Versionsnummer, setze latest_version auf null.\n"
    "Antworte ausschliesslich mit JSON nach dem Schema."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "latest_version": {"type": ["string", "null"]},
        "compatible_game_version": {"type": ["string", "null"]},
        "evidence": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["latest_version", "confidence"],
}

BLOCK_RE = re.compile(r"^##(?!#) *(.+?)\s*$(.*?)(?=^##(?!#) |\Z)",
                      re.MULTILINE | re.DOTALL)
FIELD_RE = re.compile(r"^\s*[-*]\s*\*{0,2}([A-Za-z_][\w \-]*?)\*{0,2}\s*:\s*(.+?)\s*$",
                      re.MULTILINE)
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)*)\b")
GAME_VER_RE = re.compile(r"^1\.\d+")

# Feldnamen, die eine bereits aufgeloeste Version enthalten (CurseForge/API)
LATEST_KEYS = ("latest", "neueste", "neuste", "aktuell", "file", "datei",
               "newest", "version")
INSTALLED_KEYS = ("installed", "installiert")



FNAME_V = [
    re.compile(r"[_\-\s]v[.\s]?(\d+(?:\.\d+)+)", re.I),   # _v1.43 / v3.16
    re.compile(r"[_\-\s]v[.\s]?(\d+)\b", re.I),            # _v4
    re.compile(r"[_\-\s](\d+\.\d+(?:\.\d+)*)"),           # _1.43
]
GAME_FIND_RE = re.compile(r"\b1\.\d+(?:\.\d+)*\b")


def version_from_filename(fn: str | None) -> str | None:
    """Version aus einem CurseForge-Dateinamen ziehen (lot51_core_v1.43.zip -> 1.43)."""
    if not fn:
        return None
    stem = re.sub(r"\.(zip|rar|7z|package|ts4script)$", "", fn.strip("`"), flags=re.I)
    for rx in FNAME_V:
        m = rx.search(stem)
        if m:
            return m.group(1)
    return None


def highest_tested(s: str | None) -> str | None:
    """Hoechste Spielversion aus 'zuletzt getestet mit' - numerisch, nicht als String."""
    if not s:
        return None
    cands = GAME_FIND_RE.findall(s)
    return max(cands, key=parts) if cands else None


def detect_game_version(script: Path = Path("get_game_version.py")) -> str | None:
    """Game-Version aus get_game_version.py: erst Import, sonst Subprocess."""
    if not script.exists():
        print(f"[warn] {script} nicht gefunden - Game-Abgleich uebersprungen",
              file=sys.stderr)
        return None
    try:
        sys.path.insert(0, str(script.resolve().parent))
        mod = __import__(script.stem)
        for name in ("get_game_version", "game_version", "detect"):
            fn = getattr(mod, name, None)
            if callable(fn):
                val = fn()
                if isinstance(val, str) and VERSION_RE.search(val):
                    return VERSION_RE.search(val).group(1)
                if isinstance(val, dict):
                    for v in val.values():
                        if isinstance(v, str) and VERSION_RE.search(v):
                            return VERSION_RE.search(v).group(1)
    except Exception:
        pass
    try:
        res = subprocess.run([sys.executable, str(script)],
                             capture_output=True, text=True, timeout=60)
        m = VERSION_RE.search((res.stdout or "") + (res.stderr or ""))
        if m:
            return m.group(1)
        print(f"[warn] keine Version in Ausgabe von {script}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] {script} nicht ausfuehrbar: {e}", file=sys.stderr)
    return None


def newest_collect(logdir: Path = Path("logs")) -> Path | None:
    if not logdir.is_dir():
        return None
    files = sorted(logdir.glob("collect_*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def parse_collect(path: Path) -> list[dict]:
    """
    Zerlegt die Collect-Datei in Bloecke. Alle '- key: value'-Zeilen werden
    generisch eingelesen, damit CurseForge- und HTML-Bloecke gleich behandelt
    werden koennen, egal wie check.py sie genau formatiert.
    """
    text = path.read_text(encoding="utf-8")
    blocks = []
    for m in BLOCK_RE.finditer(text):
        name, body = m.group(1).strip(), m.group(2)

        # Ueberschriften ohne Mod-Inhalt (Abschnittstitel) ignorieren
        if name.lower().startswith(("sonstige quellen", "curseforge",
                                    "zeilen mit versionsmuster", "seitentext")):
            continue

        fields = {k.strip().lower(): v.strip() for k, v in FIELD_RE.findall(body)}

        def pick(keys, exact=False):
            for k, v in fields.items():
                hit = (k in keys) if exact else any(k.startswith(p) for p in keys)
                if hit:
                    clean = v.strip("`* ").strip()
                    if clean and clean not in ("—", "-", "none", "null"):
                        return clean
            return ""

        idx = body.find("###")
        scraped = body[idx:].strip() if idx != -1 else ""

        filename = pick(LATEST_KEYS)
        blocks.append({
            "name": name,
            "installed": pick(INSTALLED_KEYS),
            "filename": filename,
            "api_latest": version_from_filename(filename),
            "release_date": pick(("datum", "date", "released", "release_date"),
                                 exact=True),
            "tested_with": highest_tested(
                fields.get("zuletzt getestet mit") or fields.get("gameversions") or ""),
            "source": fields.get("source", ""),
            "status": fields.get("status", ""),
            "scraped": scraped,
            "fields": fields,
        })
    return blocks


def norm(v) -> str | None:
    if not v:
        return None
    s = str(v).strip().lstrip("vV").strip().strip("`")
    if re.fullmatch(r"\d+[-/]\d+[-/]\d+", s):      # 2026-3-1 -> 2026.3.1
        s = re.sub(r"[-/]", ".", s)
    return s or None


def parts(s: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s)) if s else ()


def compare(installed: str, latest: str | None) -> str:
    """Deterministischer Vergleich. Kein LLM."""
    ni, nl = norm(installed), norm(latest)
    if not nl:
        return "keine_version_gefunden"
    if not ni:
        return "installed_unbekannt"
    if ni == nl:
        return "aktuell"
    pi, pl = parts(ni), parts(nl)
    # Verdacht auf abgeschnittene LLM-Ausgabe: "4" gegen "4.2"
    if pl and len(pl) < len(pi) and pi[:len(pl)] == pl:
        return "verdacht_abgeschnitten"
    try:
        vi, vl = Version(ni), Version(nl)
        return "UPDATE" if vl > vi else ("aktuell" if vl == vi else "installed_neuer")
    except InvalidVersion:
        if pi and pl:
            return "UPDATE" if pl > pi else ("aktuell" if pl == pi else "installed_neuer")
        return "manuell_pruefen"


def game_check(compat: str | None, game_version: str | None) -> str | None:
    """Nur major.minor vergleichen und nur echte Spielversionen (1.x)."""
    c, g = norm(compat), norm(game_version)
    if not c or not g or not GAME_VER_RE.match(c):
        return None
    pc, pg = parts(c)[:2], parts(g)[:2]
    if not pc or not pg:
        return None
    # Neuer getestet als die eigene Version ist unproblematisch - nur aelteres flaggen.
    return "game_version_abweichung" if pc < pg else "kompatibel"


def extract_version(scraped: str, model: str) -> dict:
    resp = ollama.chat(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": scraped[:6000]}],
        format=SCHEMA,
        options={"temperature": 0, "num_ctx": 4096},
    )
    return json.loads(resp["message"]["content"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("collect", type=Path, nargs="?", default=None)
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--game-version", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--debug", action="store_true",
                    help="zeigt die erkannten Felder je Block")
    args = ap.parse_args()

    collect = args.collect or newest_collect()
    if collect is None:
        sys.exit("Keine collect_*.md in logs/ gefunden - Pfad angeben.")
    if not collect.exists():
        sys.exit(f"Datei nicht gefunden: {collect}")
    print(f"Collect: {collect}", file=sys.stderr)

    game_version = args.game_version or detect_game_version()
    print(f"Game-Version: {game_version or 'unbekannt'}\n", file=sys.stderr)

    blocks = parse_collect(collect)
    if not blocks:
        sys.exit("Keine Mod-Bloecke gefunden - stimmt das Dateiformat?")

    if args.debug:
        for b in blocks:
            print(f"  {b['name']}\n    felder: {b['fields']}\n"
                  f"    installed={b['installed']!r} api_latest={b['api_latest']!r} "
                  f"scraped={len(b['scraped'])}", file=sys.stderr)
        print(file=sys.stderr)

    results, warnings = [], []
    for b in blocks:
        latest = compat = evidence = conf = None
        via = ""

        if b["api_latest"]:
            # CurseForge mit Version im Dateinamen -> direkt vergleichen, kein LLM
            latest, via, conf = b["api_latest"], "api", "high"
            compat = b["tested_with"]
        elif b["filename"]:
            # CurseForge ohne Version im Dateinamen: Versionsvergleich unmoeglich.
            # Aussagekraeftig ist hier nur, mit welchem Patch zuletzt getestet wurde.
            gv = game_check(b["tested_with"], game_version)
            verdict = "getestet_veraltet" if gv == "game_version_abweichung" else "getestet_aktuell"
            info = f"getestet {b['tested_with'] or '?'} | {b['release_date'] or '?'}"
            mark = "  <== nach Patch pruefen" if verdict == "getestet_veraltet" else ""
            print(f"[{verdict:<22}] {b['name']:<35} {info}{mark}", file=sys.stderr)
            results.append({**b, "latest": None, "via": "api-datum",
                            "verdict": verdict, "game_check": gv})
            continue
        elif b["scraped"] and not b["status"].lower().startswith("**"):
            try:
                ex = extract_version(b["scraped"], args.model)
            except Exception as e:
                print(f"[llm-fehler] {b['name']:<35} {e}", file=sys.stderr)
                results.append({**b, "verdict": "llm_fehler"})
                continue
            latest = ex.get("latest_version")
            compat = ex.get("compatible_game_version")
            evidence = ex.get("evidence")
            conf, via = ex.get("confidence"), "llm"
            # Halluzinationsschutz: Version muss woertlich im Text stehen
            if latest and norm(latest) and norm(latest) not in b["scraped"]:
                warnings.append(f"{b['name']}: '{latest}' steht nicht im Seitentext")
                conf = "low"
        else:
            print(f"[skip] {b['name']:<35} ({b['status'] or 'keine Daten'})",
                  file=sys.stderr)
            results.append({**b, "verdict": "uebersprungen"})
            continue

        verdict = compare(b["installed"], latest)
        gv = game_check(compat, game_version)

        flag = "  <== UPDATE" if verdict == "UPDATE" else ""
        if verdict in ("verdacht_abgeschnitten", "manuell_pruefen") or conf == "low":
            flag += "  (?)"
        if gv == "game_version_abweichung":
            flag += f"  [{gv}]"
        print(f"[{verdict:<22}] {b['name']:<35} "
              f"{b['installed'] or '—':<12} -> {latest or '—':<10} {via}{flag}",
              file=sys.stderr)

        results.append({**b, "latest": latest, "via": via, "evidence": evidence,
                        "compatible_game_version": compat, "confidence": conf,
                        "verdict": verdict, "game_check": gv})

    updates = [r for r in results if r["verdict"] == "UPDATE"]
    check = [r for r in results if r["verdict"] in
             ("manuell_pruefen", "keine_version_gefunden", "verdacht_abgeschnitten",
              "installed_neuer", "installed_unbekannt")]
    stale = [r for r in results if r["verdict"] == "getestet_veraltet"]
    skipped = [r for r in results if r["verdict"] == "uebersprungen"]

    print(f"\n{len(updates)} Update(s), {len(check)} pruefen, "
          f"{len(stale)} vor aktuellem Patch getestet, "
          f"{len(skipped)} uebersprungen, {len(results)} gesamt.", file=sys.stderr)
    for w in warnings:
        print(f"  [warn] {w}", file=sys.stderr)

    if args.out:
        args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        print(f"Ergebnis: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()