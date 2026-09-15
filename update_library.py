#!/usr/bin/env python3
"""
Fetch the live Raven360 English course catalog and update AWS_Content_Library.html.
Designed to run inside the aws-content-library/ repo directory (locally or in CI).

Credentials are read from env vars:
  RAVEN360_CLIENT_ID  RAVEN360_CLIENT_SECRET  RAVEN360_XAPI_KEY
A local .env file in the same directory is loaded if it exists (dev convenience).
"""

import os
import re
import sys
import json
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

RAVEN360_BASE_URL      = "https://api.raven360.com"
RAVEN360_CLIENT_ID     = os.environ.get("RAVEN360_CLIENT_ID")
RAVEN360_CLIENT_SECRET = os.environ.get("RAVEN360_CLIENT_SECRET")
RAVEN360_XAPI_KEY      = os.environ.get("RAVEN360_XAPI_KEY")

LIBRARY_HTML = Path(__file__).parent / "AWS_Content_Library.html"
DATA_START   = "<!-- COURSE_DATA_START -->"
DATA_END     = "<!-- COURSE_DATA_END -->"

_LANG_HINT_RE = re.compile(
    r'\('
    r'Español|Français|Deutsch|Português|Italiano|Nederlands|Türkçe|Polski|'
    r'Svenska|Norsk|Dansk|Suomi|Bahasa|Tiếng Việt|Čeština|Română|Magyar|'
    r'日本語|한국어|中文|繁體|简体|العربية|עברית|हिन्दी|ภาษาไทย|Русский|Ελληνικά|'
    r'Vietnamese|Korean|Chinese|Japanese|Thai|Arabic|Hindi|Russian|Portuguese|'
    r'Spanish|French|German|Italian|Dutch|Turkish|Polish|Swedish|Norwegian|'
    r'Danish|Finnish|Czech|Romanian|Hungarian|Greek|Hebrew'
    r'\)',
    re.IGNORECASE
)
_NON_LATIN = {
    "CJK", "ARABIC", "HEBREW", "CYRILLIC", "HANGUL",
    "HIRAGANA", "KATAKANA", "THAI", "DEVANAGARI", "GEORGIAN", "ARMENIAN", "GREEK"
}


def _has_non_latin(text):
    for char in text:
        if ord(char) < 128:
            continue
        if any(s in unicodedata.name(char, "") for s in _NON_LATIN):
            return True
    return False


def _is_english(item):
    for cat in (item.get("category") or []):
        if cat.get("title") == "Language":
            for tag in (cat.get("tags") or []):
                if tag.get("assigned") and tag.get("title") != "English":
                    return False
            break
    name = item.get("name", "") or ""
    return not _has_non_latin(name) and not _LANG_HINT_RE.search(name)


def _extract_cid(url):
    if not url:
        return None
    cids = parse_qs(urlparse(url).query).get("cid", [])
    return cids[0] if cids else None


def _assigned_tags(item, category_title):
    for cat in (item.get("category") or []):
        if cat.get("title") == category_title:
            return [t.get("title") for t in (cat.get("tags") or []) if t.get("assigned")]
    return []


_TFC_PREFIX_RE = re.compile(
    r'^\*This course was developed by members of AWS Technical Field Communities.*?\.\*\s*',
    re.DOTALL
)
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_MD_EMPHASIS_RE = re.compile(r'\*\*?([^*]+)\*\*?')


def clean_description(desc):
    desc = (desc or "").strip()
    desc = _TFC_PREFIX_RE.sub("", desc)
    desc = _MD_LINK_RE.sub(r"\1", desc)
    desc = _MD_EMPHASIS_RE.sub(r"\1", desc)
    return re.sub(r"\s+", " ", desc).strip()


def main():
    missing = [v for v in ("RAVEN360_CLIENT_ID", "RAVEN360_CLIENT_SECRET", "RAVEN360_XAPI_KEY")
               if not os.environ.get(v)]
    if missing:
        print(f"FATAL: Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    print("Authenticating with Raven360 ...")
    token_resp = requests.post(
        f"{RAVEN360_BASE_URL}/gettoken",
        json={"client_id": RAVEN360_CLIENT_ID, "client_secret": RAVEN360_CLIENT_SECRET},
        headers={"x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"},
        timeout=30
    )
    token_resp.raise_for_status()
    token = token_resp.json()["data"]["token"]

    print("Fetching live catalog ...")
    catalog_resp = requests.post(
        f"{RAVEN360_BASE_URL}/administration/catalog/learningobjects",
        headers={"Authorization": token, "x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"},
        json={"learningobject_type": "Content",
              "from_date": "01-01-2020",
              "to_date": date.today().strftime("%m-%d-%Y")},
        timeout=120
    )
    catalog_resp.raise_for_status()
    items = catalog_resp.json().get("data", [])

    records = []
    for item in items:
        if not _is_english(item):
            continue
        url = item.get("launch_url", "")
        if not _extract_cid(url):
            continue
        domains = _assigned_tags(item, "Domain")
        levels  = _assigned_tags(item, "Skill Level")
        records.append({
            "name":        (item.get("display_name") or item.get("name") or "").strip(),
            "description": clean_description(item.get("short_description")),
            "url":         url,
            "category":    domains[0] if domains else "General",
            "level":       levels[0] if levels else "Fundamental",
        })
    records.sort(key=lambda r: (r["category"].lower(), r["name"].lower()))
    print(f"  {len(records)} English courses exported")

    if not LIBRARY_HTML.exists():
        print(f"FATAL: {LIBRARY_HTML} not found")
        sys.exit(1)

    html = LIBRARY_HTML.read_text(encoding="utf-8")
    if DATA_START not in html or DATA_END not in html:
        print(f"FATAL: data markers not found in {LIBRARY_HTML.name}")
        sys.exit(1)

    payload = json.dumps(records, indent=2, ensure_ascii=False)
    new_block = (
        f"{DATA_START}\n"
        f'<script type="application/json" id="course-data">\n{payload}\n</script>\n'
        f"{DATA_END}"
    )
    pattern = re.compile(re.escape(DATA_START) + r".*?" + re.escape(DATA_END), re.DOTALL)
    html = pattern.sub(lambda _m: new_block, html, count=1)
    LIBRARY_HTML.write_text(html, encoding="utf-8")
    print(f"  Updated {LIBRARY_HTML.name}")


if __name__ == "__main__":
    main()
