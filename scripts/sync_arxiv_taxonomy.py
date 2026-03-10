#!/usr/bin/env python3
"""Sync arXiv category taxonomy into local JSON knowledge base."""

from __future__ import annotations

import argparse
import json
import re
import html
from pathlib import Path
from urllib.request import Request, urlopen


TAXONOMY_URL = "https://arxiv.org/category_taxonomy"


def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "agent-daily-paper/1.0"})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_entries(html: str) -> list[dict[str, str]]:
    # Typical taxonomy row:
    # <h4>cs.AI <span>(Artificial Intelligence)</span></h4> ... <p>description</p>
    pattern = re.compile(
        r"<h4>\s*([^<\s]+)\s*<span>\(([^)]+)\)</span>\s*</h4>\s*</div>\s*<div class=\"column\"><p>(.*?)</p>",
        re.IGNORECASE | re.DOTALL,
    )
    seen: set[str] = set()
    entries: list[dict[str, str]] = []
    for code_raw, name_raw, desc_raw in pattern.findall(html):
        code = code_raw.strip()
        name = html_unescape(name_raw.strip())
        desc = html_unescape(_strip_tags(desc_raw))
        if code in seen:
            continue
        seen.add(code)
        group = code.split(".", 1)[0]
        entries.append(
            {
                "code": code,
                "name": name,
                "group": group,
                "description": re.sub(r"\s+", " ", desc).strip(),
            }
        )

    # Fallback: lightweight extraction if page structure changes.
    if not entries:
        fallback = re.compile(r"\b([a-z][a-z\-]*(?:\.[A-Za-z][A-Za-z\-]*)?)\b\s*\(([^)]+)\)")
        for code_raw, name_raw in fallback.findall(html):
            code = code_raw.strip()
            if code in seen:
                continue
            seen.add(code)
            group = code.split(".", 1)[0]
            entries.append({"code": code, "name": html_unescape(name_raw.strip()), "group": group, "description": ""})

    entries.sort(key=lambda x: x["code"])
    return entries


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def html_unescape(s: str) -> str:
    return html.unescape(s or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync arXiv category taxonomy to local JSON")
    parser.add_argument("--url", default=TAXONOMY_URL)
    parser.add_argument("--output", default="data/arxiv_taxonomy.json")
    args = parser.parse_args()

    html = fetch_html(args.url)
    entries = extract_entries(html)
    out = {
        "source_url": args.url,
        "entry_count": len(entries),
        "entries": entries,
    }

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] taxonomy saved: {path} (entries={len(entries)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
