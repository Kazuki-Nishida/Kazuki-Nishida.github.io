#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the CV (web page + PDF) from cv.yaml and researchmap.

    python cv/build_cv.py            build from cv.yaml + the saved researchmap data
    python cv/build_cv.py --fetch    refresh the researchmap data first (needs internet)
    python cv/build_cv.py --no-pdf   web page only
    python cv/build_cv.py --force    rebuild even if nothing changed
    python cv/build_cv.py --check    report what would change, write nothing

Outputs (only rewritten when the content really changed):
    cv/index.html, cv/<pdf_name>, cv/data/state.json,
    and the date between <!--cv-updated--> markers on the top pages.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

CV = Path(__file__).resolve().parent
ROOT = CV.parent
DATA = CV / "data"
CACHE = DATA / "researchmap_papers.json"
STATE = DATA / "state.json"
JST = dt.timezone(dt.timedelta(hours=9))
API = "https://api.researchmap.jp/{permalink}/published_papers?limit={limit}&start={start}"
UPDATED_LONG = "@@CV_UPDATED_LONG@@"
UPDATED_ISO = "@@CV_UPDATED_ISO@@"
MAX_DROP = 3          # refuse a refresh that loses more papers than this (protects against a broken API reply)
HEARTBEAT_DAYS = 30   # touch state.json at least this often so GitHub keeps the weekly schedule alive
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"  ! {msg}")


# ----------------------------------------------------------------- text utils
_SPACES = re.compile("[    ​ ]")


def clean(s) -> str:
    """NFC, ordinary spaces, single-spaced, trimmed."""
    if s is None:
        return ""
    s = _SPACES.sub(" ", unicodedata.normalize("NFC", str(s)))
    return re.sub(r"\s+", " ", s).strip()


def md_inline(s) -> Markup:
    """Escape, then turn *text* into italics."""
    return Markup(re.sub(r"\*(.+?)\*", r"<i>\1</i>", str(escape(clean(s)))))


def domain(url: str) -> str:
    host = urlparse(url).netloc
    return host[4:] if host.startswith("www.") else host


def long_date(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


# ------------------------------------------------------------ researchmap data
def _pick(v) -> str:
    """researchmap text fields are {"en": ..., "ja": ...}."""
    if isinstance(v, dict):
        return v.get("en") or v.get("ja") or next(iter(v.values()), "")
    return v or ""


def reduce_item(it: dict) -> dict:
    """Keep only what the CV needs, so the saved file changes only when a paper does."""
    authors = it.get("authors") or {}
    names = (authors.get("en") or authors.get("ja") or []) if isinstance(authors, dict) else authors
    dois = (it.get("identifiers") or {}).get("doi") or []
    return {
        "rm:id": str(it.get("rm:id", "")),
        "paper_title": clean(_pick(it.get("paper_title"))),
        "authors": [clean(a.get("name")) for a in names if clean(a.get("name"))],
        "publication_name": clean(_pick(it.get("publication_name"))),
        "publication_date": clean(it.get("publication_date")),
        "volume": clean(it.get("volume")),
        "number": clean(it.get("number")),
        "starting_page": clean(it.get("starting_page")),
        "ending_page": clean(it.get("ending_page")),
        "doi": clean(dois[0]) if dois else "",
        "roles": list(it.get("published_paper_owner_roles") or []),
        "type": it.get("published_paper_type") or "scientific_journal",
    }


def fetch_researchmap(permalink: str) -> list[dict]:
    items: list[dict] = []
    total = None
    while total is None or len(items) < total:
        url = API.format(permalink=permalink, limit=1000, start=len(items) + 1)
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "statsbio-cv-builder (+https://statsbio.com)"})
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                break
            except Exception as e:  # noqa: BLE001 - any network/JSON problem is retried
                last_err = e
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"researchmap request failed: {last_err}")
        batch = data.get("items") or []
        total = int(data.get("total_items", len(batch)))
        if not batch:
            break
        items += batch
    return [reduce_item(i) for i in items if i.get("display", "disclosed") == "disclosed"]


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def refresh_cache(permalink: str, today: dt.date, write: bool = True) -> str:
    """Fetch researchmap and update the saved copy. Never replaces good data with a broken reply."""
    old = load_json(CACHE, None)
    try:
        items = fetch_researchmap(permalink)
    except Exception as e:  # noqa: BLE001
        warn(f"researchmap could not be read ({e}); keeping the saved data")
        return "kept"
    if not items or (old and len(items) < len(old["items"]) - MAX_DROP):
        warn(f"researchmap returned {len(items)} papers (saved: {len(old['items']) if old else 0}); "
             "this looks wrong, keeping the saved data")
        return "kept"
    items.sort(key=lambda x: (x["publication_date"], x["rm:id"]), reverse=True)
    if old and old.get("items") == items:
        return "same"
    if write:
        DATA.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps({
            "source": API.split("?")[0].format(permalink=permalink),
            "fetched": today.isoformat(), "total_items": len(items), "items": items,
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return "updated"


# ------------------------------------------------------------- tidying records
ROLE_MAP = {"lead": "first", "corresponding": "corresponding", "last": "last"}
SMALL_WORDS = {"of", "and", "the", "in", "for", "on", "to", "a", "an", "et", "de", "la", "und", "der", "des", "du"}


def tidy_name(name: str, fixes: dict) -> str:
    n = clean(name)
    if n in fixes:
        return fixes[n]
    if "," in n:                                   # "Family, Given" -> "Given Family"
        family, given = (x.strip() for x in n.split(",", 1))
        n = f"{given} {family}".strip()
    if n == n.lower():                             # "hiroaki hiraiwa"
        n = n.title()
    return fixes.get(n, n)


def _cap(word: str) -> str:
    return "-".join(p[:1].upper() + p[1:] for p in word.split("-"))


def tidy_journal(raw: str, names: dict) -> str:
    s = clean(raw)
    if s.casefold() in names:
        return names[s.casefold()]
    s = re.sub(r"\s*\([^()]*(\([^()]*\))?[^()]*\)\s*$", "", s)   # "Shock (Augusta, Ga.)"
    s = re.split(r"\s+:\s+|;\s+", s)[0]                           # " : official journal of ..."
    if s.casefold() in names:
        return names[s.casefold()]
    words = s.split(" ")
    shouting = s.upper() == s and len(words) > 1
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if not shouting and len(w) > 1 and w.upper() == w and any(c.isalpha() for c in w):
            out.append(w)                          # acronyms: BMC, BMJ, ASAIO
        elif i and low in SMALL_WORDS:
            out.append(low)
        else:
            out.append(_cap(low))
    return " ".join(out)


def tidy_title(raw: str) -> str:
    t = clean(raw)
    while t.endswith("."):
        t = t[:-1].rstrip()
    if t.upper() == t and any(c.isalpha() for c in t):   # ALL CAPS -> sentence case
        t = t[:1] + t[1:].lower()
    return t


def date_key(d: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", d or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))      # year-only entries sort last within their year


def source_line(year: int, volume: str, number: str, sp: str, ep: str) -> str:
    vol = volume if re.fullmatch(r"\d+[A-Za-z]?", volume or "") else ""
    m = re.match(r"\d+", number or "")
    num = m.group(0) if m else ""
    if re.fullmatch(r"\d{9,}", sp or "") or sp == "1":   # DOI fragments / ahead-of-print page "1"
        sp = ep = ""
    sp = (sp or "").replace("-", "–")
    pages = sp if (not ep or ep == sp) else f"{sp}–{ep}"
    s = str(year) if year else "in press"
    if vol:
        s += f";{vol}" + (f"({num})" if num else "") + (f":{pages}" if pages else "")
    return s


def role_label(roles) -> str:
    parts = []
    if "co-first" in roles:
        parts.append("co-first")
    elif "first" in roles:
        parts.append("first")
    if "last" in roles:
        parts.append("last")
    if "corresponding" in roles:
        parts.append("corresponding")
    if not parts:
        return ""
    text = " and ".join(parts) + " author"
    return text[:1].upper() + text[1:]


def authors_html(names: list[str], own: list[int], limit: int | None) -> Markup:
    def one(i: int) -> Markup:
        n = escape(names[i])
        return Markup("<strong>%s</strong>") % n if i in own else n

    n = len(names)
    if not limit or n <= limit:
        return Markup(", ").join(one(i) for i in range(n))
    me = own[0] if own else None
    parts = [one(0), one(1), one(2)]
    if me is not None and 3 <= me < n - 1:
        if me > 3:
            parts.append(Markup("…"))
        parts.append(one(me))
        if me < n - 2:
            parts.append(Markup("…"))
    else:
        parts.append(Markup("…"))
    parts.append(one(n - 1))
    return Markup(", ").join(parts) + Markup(' <span class="n-auth">(%d authors)</span>') % n


def build_publications(cfg: dict, cache: dict):
    pc = cfg["publications"]
    fixes = {clean(k): v for k, v in (pc.get("author_name_fixes") or {}).items()}
    jnames = {clean(k).casefold(): v for k, v in (pc.get("journal_names") or {}).items()}
    own_names = {clean(n).casefold() for n in pc["own_names"]}
    excluded = {str(x).casefold() for x in (pc.get("exclude") or [])}
    role_over = {str(k).casefold(): v for k, v in (pc.get("roles") or {}).items()}
    overrides = {str(k).casefold(): v for k, v in (pc.get("overrides") or {}).items()}
    limit = pc.get("max_authors")

    recs: dict[str, dict] = {}
    for r in cache.get("items", []):
        rid = f"rm:{r['rm:id']}"
        doi = clean(r.get("doi")).casefold()
        if rid in excluded or (doi and doi in excluded):
            continue
        rec = {"title": r["paper_title"], "authors": list(r["authors"]), "journal": r["publication_name"],
               "date": r["publication_date"], "volume": r.get("volume", ""), "number": r.get("number", ""),
               "sp": r.get("starting_page", ""), "ep": r.get("ending_page", ""), "doi": doi,
               "roles": [ROLE_MAP.get(x, x) for x in r.get("roles", [])], "key": doi or rid}
        for k, v in (overrides.get(doi) or overrides.get(rid) or {}).items():
            if k == "pages":
                rec["sp"], rec["ep"] = str(v), ""
            else:
                rec[k] = list(v) if isinstance(v, list) else str(v)
        if doi in role_over:
            rec["roles"] = list(role_over[doi])
        recs[rec["key"]] = rec

    for e in pc.get("extra") or []:
        doi = clean(e.get("doi")).casefold()
        rec = {"title": e["title"], "authors": list(e["authors"]), "journal": e["journal"],
               "date": str(e["date"]), "volume": str(e.get("volume", "")), "number": str(e.get("number", "")),
               "sp": str(e.get("pages", "")), "ep": "", "doi": doi,
               "roles": list(e.get("roles") or []), "key": doi or f"extra:{clean(e['title']).casefold()}"}
        if doi and doi in recs:        # now on researchmap too: the hand-checked entry wins
            rec["roles"] = rec["roles"] or recs[doi]["roles"]
        recs[rec["key"]] = rec

    pubs = []
    for rec in recs.values():
        names = [tidy_name(a, fixes) for a in rec["authors"]]
        own = [i for i, n in enumerate(names) if n.casefold() in own_names]
        title = tidy_title(rec["title"])
        if len(own) != 1:
            warn(f"own name found {len(own)} times in the author list of: {title[:80]}")
        y, m, d = date_key(rec["date"])
        lead = bool(set(rec["roles"]) & {"first", "co-first", "corresponding", "last"})
        pubs.append(SimpleNamespace(
            key=rec["key"], doi=rec["doi"], title=title,
            title_end="" if title.endswith(("?", "!")) else ".",
            journal=tidy_journal(rec["journal"], jnames), year=y, sort=(y, m, d),
            source=source_line(y, rec["volume"], rec["number"], rec["sp"], rec["ep"]),
            roles=rec["roles"], role_label=role_label(rec["roles"]), lead=lead,
            authors=authors_html(names, own, None if lead else limit), n_authors=len(names)))
    pubs.sort(key=lambda p: (p.sort, p.title), reverse=True)

    def with_years(seq):
        prev = None
        for p in seq:
            p.show_year = p.year != prev
            prev = p.year
        return seq

    lead = with_years([p for p in pubs if p.lead])
    coauth = with_years([p for p in pubs if not p.lead])
    return lead, coauth


def build_preprints(cfg: dict):
    pc = cfg["publications"]
    fixes = {clean(k): v for k, v in (pc.get("author_name_fixes") or {}).items()}
    own_names = {clean(n).casefold() for n in pc["own_names"]}
    out = []
    for e in cfg.get("preprints") or []:
        names = [tidy_name(a, fixes) for a in e["authors"]]
        own = [i for i, n in enumerate(names) if n.casefold() in own_names]
        label = role_label(e.get("roles") or [])
        if e.get("status"):
            label = f"{label}; {e['status']}" if label else e["status"]
        if len(own) != 1:
            warn(f"own name found {len(own)} times in the author list of preprint: {e['title'][:80]}")
        out.append(SimpleNamespace(title=tidy_title(e["title"]), authors=authors_html(names, own, None),
                                   venue=e["venue"], year=e["year"], url=e.get("url", ""), role_label=label,
                                   show_year=not out or out[-1].year != e["year"]))
    return out


# -------------------------------------------------------------------- outputs
def render(cfg: dict, lead, coauth, preprints, short: bool = False) -> str:
    env = Environment(loader=FileSystemLoader(str(CV)), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["md"] = md_inline
    env.filters["domain"] = domain
    return env.get_template("template.html").render(
        meta=cfg["meta"], p=cfg["person"], cfg=cfg, lead=lead, coauth=coauth, preprints=preprints,
        n_total=len(lead) + len(coauth), n_lead=len(lead), short=short,
        updated_long=UPDATED_LONG, updated_iso=UPDATED_ISO)


def find_chrome() -> str | None:
    for c in (os.environ.get("CHROME"), "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if c and (shutil.which(c) or Path(c).exists()):
            return shutil.which(c) or c
    return None


def make_pdf(html: Path, pdf: Path) -> None:
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("Chrome/Chromium not found (set CHROME=/path/to/chrome)")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cv.pdf"
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--no-pdf-header-footer", "--virtual-time-budget=10000",
                        f"--user-data-dir={tmp}/profile", f"--print-to-pdf={out}", html.resolve().as_uri()],
                       check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not out.exists() or out.stat().st_size < 20_000:
            raise RuntimeError("Chrome did not produce a usable PDF")
        shutil.copyfile(out, pdf)


def stamp_pages(cfg: dict, values: dict[str, str]) -> list[str]:
    """Rewrite the text between <!--name--> ... <!--/name--> markers on the listed pages."""
    changed = []
    for rel in cfg["meta"].get("stamp_pages") or []:
        f = ROOT / rel
        if not f.exists():
            continue
        old = new = f.read_text(encoding="utf-8")
        for name, value in values.items():
            new = re.sub(rf"(<!--{name}-->).*?(<!--/{name}-->)",
                         lambda m, v=value: m.group(1) + v + m.group(2), new, flags=re.S)
        if new != old:
            f.write_text(new, encoding="utf-8")
            changed.append(rel)
    return changed


def digest(*chunks: bytes) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.hexdigest()[:20]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="refresh the researchmap data first")
    ap.add_argument("--no-pdf", action="store_true", help="skip the PDF")
    ap.add_argument("--force", action="store_true", help="rebuild even if nothing changed")
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (for testing)")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(JST).date()
    cfg = yaml.safe_load((CV / "cv.yaml").read_text(encoding="utf-8"))
    state = load_json(STATE, {})

    if args.fetch:
        result = refresh_cache(cfg["publications"]["researchmap_permalink"], today, write=not args.check)
        print(f"researchmap: {result}")
    cache = load_json(CACHE, {"items": []})
    if not cache["items"]:
        warn("no researchmap data saved yet (run with --fetch); only cv.yaml entries are listed")

    lead, coauth = build_publications(cfg, cache)
    preprints = build_preprints(cfg)
    html = render(cfg, lead, coauth, preprints)
    css = (CV / "cv.css").read_bytes()
    content_hash = digest(html.encode())          # what the CV says
    build_hash = digest(html.encode(), css)       # ... and how it looks

    keys = sorted(p.key for p in lead + coauth)
    added = [p for p in lead + coauth if state.get("keys") and p.key not in set(state["keys"])]
    removed = sorted(set(state.get("keys") or []) - set(keys))
    pdf = CV / cfg["meta"]["pdf_name"]
    pdf_short = CV / cfg["meta"]["pdf_short_name"] if cfg["meta"].get("pdf_short_name") else None
    page = CV / "index.html"
    content_changed = content_hash != state.get("content_hash")
    stale = (args.force or build_hash != state.get("build_hash") or not page.exists()
             or (not args.no_pdf and not (pdf.exists() and (not pdf_short or pdf_short.exists()))))

    print(f"publications: {len(lead) + len(coauth)} ({len(lead)} lead-author), preprints: {len(preprints)}")
    for p in added:
        print(f"  + {p.title[:90]}")
    for k in removed:
        print(f"  - {k}")

    if args.check:
        print("would rebuild" if stale else "nothing to do")
        return 0

    updated = today if (content_changed or not state.get("updated")) else dt.date.fromisoformat(state["updated"])
    if stale:
        def dated(text: str) -> str:
            return text.replace(UPDATED_LONG, long_date(updated)).replace(UPDATED_ISO, updated.isoformat())

        tmp_page = CV / ".index.building.html"       # next to cv.css and fonts/, so relative links work
        tmp_short = CV / ".short.building.html"
        try:
            tmp_page.write_text(dated(html), encoding="utf-8")
            if not args.no_pdf:
                make_pdf(tmp_page, pdf)
                if pdf_short:
                    tmp_short.write_text(dated(render(cfg, lead, coauth, preprints, short=True)), encoding="utf-8")
                    make_pdf(tmp_short, pdf_short)
            tmp_page.replace(page)
        finally:
            tmp_page.unlink(missing_ok=True)
            tmp_short.unlink(missing_ok=True)
        print(f"built: {page.relative_to(ROOT)}" + ("" if args.no_pdf else " and PDF")
              + f" (updated {updated.isoformat()})")
    else:
        print("CV unchanged")

    stamped = stamp_pages(cfg, {"cv-updated": updated.strftime("%Y.%m.%d"),
                                "pub-total": str(len(lead) + len(coauth))})
    if stamped:
        print("stamped: " + ", ".join(stamped))

    checked = state.get("checked")
    heartbeat = not checked or (today - dt.date.fromisoformat(checked)).days >= HEARTBEAT_DAYS
    new_state = {"updated": updated.isoformat(), "content_hash": content_hash, "build_hash": build_hash,
                 "checked": today.isoformat() if (heartbeat or stale) else checked,
                 "n_publications": len(keys), "n_lead": len(lead), "keys": keys}
    if new_state != state:
        DATA.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(new_state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    if os.environ.get("GITHUB_OUTPUT"):
        note = f"+{len(added)} paper(s)" if added else ("content" if content_changed else "refresh")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"note={note}\nupdated={updated.isoformat()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
