#!/usr/bin/env python3
"""Refresh the managed publication list in writing.html from Airtable.

Pull-only. Reads the Content table, keeps rows that are (a) published, (b) authored
by me, and (c) carry a real URL, then rewrites the block between the marker
comments in writing.html. Everything outside the markers is hand-written and is
never touched.

The base is messy and its field names are unstable, so the reader is deliberately
permissive: for each thing we need there is a list of candidate field names and
the first one present wins. This mirrors how machine/src/pe/distribute/assets.py
reads the same table in Operation Change.

Stdlib only, so the GitHub Actions job needs no pip install.

Env:
  AIRTABLE_API_KEY   required (the only real secret)
  AIRTABLE_BASE_ID   default appU6vYAsEYaZ7hNL — an identifier, not a secret
  AIRTABLE_CONTENT_TABLE  default tbltnq78edYwEh850
  WRITING_AUTHOR_MATCH    default "kaspar" (case-insensitive substring)
  WRITING_MAX_ITEMS       default 12
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.airtable.com/v0"

REPO = Path(__file__).resolve().parent.parent
PAGE = REPO / "writing.html"

START = "<!-- publications:start -->"
END = "<!-- publications:end -->"

# First present field wins, so a rename in Airtable does not break the pull.
TITLE_FIELDS = ["Title", "Name", "Headline", "Piece"]
LINK_FIELDS = ["Published URL", "Link", "URL", "Url"]
AUTHOR_FIELDS = ["Author", "Authors", "Author (text)", "Owner", "Owner (text)",
                 "Writer", "Byline", "Created by"]
DATE_FIELDS = ["Publish date", "Published timestamp", "Distribution date", "Start date"]
NOTE_FIELDS = ["Description", "Essence/Intention", "Notes/context", "Notes"]
STATUS_FIELD = "Status"

# A one-line gloss after the title, matching the hand-written entries. Long
# Airtable descriptions are dropped rather than truncated mid-sentence.
MAX_NOTE_CHARS = 160

# Only pieces real enough to point at. Ideas, drafts, in-review and on-hold are noise.
READY_STATUSES = {"published", "done", "push to publishing", "push to distro",
                  "ready for publication"}

URL_RE = re.compile(r"https?://[^\s\"'<>]+")


# ── Airtable ────────────────────────────────────────────────────────────────

def list_records(api_key: str, base: str, table: str) -> list[dict]:
    """Every record in the table, following pagination."""
    out: list[dict] = []
    offset = None
    while True:
        params = [("pageSize", "100")]
        if offset:
            params.append(("offset", offset))
        url = f"{API}/{base}/{urllib.parse.quote(table)}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            raise SystemExit(f"Airtable {e.code}: {body}")
        out.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return out


# ── Field readers ───────────────────────────────────────────────────────────

def pick(fields: dict, names: list[str]) -> str | None:
    for n in names:
        v = fields.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def flatten(v) -> list[str]:
    """An Airtable value as a list of strings. Handles plain text, multi-selects,
    lookups, and collaborator objects ({"name": ..., "email": ...})."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [str(v.get("name") or v.get("email") or "")]
    if isinstance(v, list):
        out = []
        for item in v:
            out.extend(flatten(item))
        return out
    return [str(v)]


def authors(fields: dict) -> list[str]:
    """Every author-ish value on the row, across all candidate field names. Unlike
    pick() this does not stop at the first hit: a row may carry both an "Author"
    text field and a "Created by" collaborator, and either may be the one naming me."""
    out: list[str] = []
    for n in AUTHOR_FIELDS:
        if n in fields:
            out.extend(s for s in flatten(fields[n]) if s.strip())
    return out


def as_url(v) -> str | None:
    """Coerce a field value into a real URL, or None. A full embedded URL wins (the
    last one, which is the most specific). A bare host/path is assumed https. Free
    text such as "TBD" returns None, so we never fabricate https://<junk>."""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    embedded = URL_RE.findall(s)
    if embedded:
        return embedded[-1]
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/[^\s]*)?$", s, re.I):
        return "https://" + s
    return None


# ── Selection ───────────────────────────────────────────────────────────────

def collect(records: list[dict], author_match: str, limit: int) -> list[dict]:
    """Published, mine, linkable. Most recent first, deduped by URL."""
    needle = author_match.lower()
    rows: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        f = rec.get("fields", {})

        status = str(f.get(STATUS_FIELD) or "").strip().lower()
        if status not in READY_STATUSES:
            continue

        if not any(needle in a.lower() for a in authors(f)):
            continue

        title = pick(f, TITLE_FIELDS)
        if not title:
            continue

        url = as_url(pick(f, LINK_FIELDS))
        if not url:
            continue  # a writing list entry with no link is not useful

        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)

        when = (pick(f, DATE_FIELDS) or str(rec.get("createdTime") or ""))[:10]

        note = (pick(f, NOTE_FIELDS) or "").strip()
        note = " ".join(note.split())          # collapse newlines from long-text cells
        if len(note) > MAX_NOTE_CHARS:
            note = ""                          # a cut-off gloss reads worse than none
        note = note.rstrip(" .")

        rows.append({
            "title": title[:200],
            "url": url,
            "date": when,
            "note": note,
        })

    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows[:limit]


# ── Rendering ───────────────────────────────────────────────────────────────

def render(rows: list[dict]) -> str:
    if not rows:
        # Leave the section honest rather than showing an empty list.
        return "            <p>Nothing new to list right now.</p>"

    # Matches the hand-written entries elsewhere on the page: italic linked title,
    # then a hyphen and a one-line gloss.
    lines = ["            <ul>"]
    for r in rows:
        title = html.escape(r["title"])
        url = html.escape(r["url"], quote=True)
        gloss = f" - {html.escape(r['note'])}" if r["note"] else ""
        lines.append(
            f'                <li><em><a href="{url}" target="_blank" rel="noopener">'
            f'{title}</a></em>{gloss}</li>'
        )
    lines.append("            </ul>")
    return "\n".join(lines)


def splice(page: str, block: str) -> str:
    start = page.find(START)
    end = page.find(END)
    if start == -1 or end == -1 or end < start:
        raise SystemExit(
            f"markers not found in {PAGE.name}: expected {START} ... {END}"
        )
    # Re-indent the closing marker to match the opening one, so repeated runs do
    # not slowly walk it to column zero.
    line_start = page.rfind("\n", 0, start) + 1
    indent = page[line_start:start]
    return page[: start + len(START)] + "\n" + block + "\n" + indent + page[end:]


# ── Diagnostics ─────────────────────────────────────────────────────────────

def diagnose(records: list[dict], author_match: str) -> None:
    """Explain a zero-match run so the fix is visible in the Actions log without
    anyone having to query the base by hand. Prints field NAMES and counts only,
    never cell values, so the log stays safe to share."""
    if not records:
        print("  the table returned no records at all — check the base/table id",
              file=sys.stderr)
        return

    names: dict[str, int] = {}
    for rec in records:
        for k in rec.get("fields", {}):
            names[k] = names.get(k, 0) + 1

    ready = sum(1 for r in records
                if str(r.get("fields", {}).get(STATUS_FIELD) or "").strip().lower()
                in READY_STATUSES)
    print(f"  {ready}/{len(records)} rows pass the status gate", file=sys.stderr)

    seen_author = [n for n in AUTHOR_FIELDS if n in names]
    print(f"  author fields present: {seen_author or 'NONE — this is the likely fault'}",
          file=sys.stderr)
    if seen_author:
        hits = sum(1 for r in records
                   if any(author_match.lower() in a.lower()
                          for a in authors(r.get("fields", {}))))
        print(f"  rows whose author matches '{author_match}': {hits}", file=sys.stderr)

    print(f"  link fields present: {[n for n in LINK_FIELDS if n in names] or 'NONE'}",
          file=sys.stderr)
    print(f"  title fields present: {[n for n in TITLE_FIELDS if n in names] or 'NONE'}",
          file=sys.stderr)
    print("  all field names on the table (name: rows populated):", file=sys.stderr)
    for n, c in sorted(names.items(), key=lambda kv: -kv[1]):
        print(f"    {n!r}: {c}", file=sys.stderr)


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    api_key = os.environ.get("AIRTABLE_API_KEY", "")
    if not api_key:
        raise SystemExit("AIRTABLE_API_KEY must be set")

    # Base and table ids are identifiers, not secrets, so they carry defaults and
    # only need overriding if the content team moves the table.
    base = os.environ.get("AIRTABLE_BASE_ID") or "appU6vYAsEYaZ7hNL"
    table = os.environ.get("AIRTABLE_CONTENT_TABLE", "tbltnq78edYwEh850")
    author_match = os.environ.get("WRITING_AUTHOR_MATCH", "kaspar")
    limit = int(os.environ.get("WRITING_MAX_ITEMS", "12"))

    records = list_records(api_key, base, table)
    rows = collect(records, author_match, limit)
    print(f"{len(records)} records in {table}, {len(rows)} published pieces by "
          f"'{author_match}' with a link", file=sys.stderr)

    # A pull that finds nothing is far more likely to be a broken field name or a
    # revoked token than a real empty result, so refuse to wipe a good page.
    if not rows:
        print("no matching pieces; leaving writing.html untouched", file=sys.stderr)
        diagnose(records, author_match)
        return 0

    page = PAGE.read_text(encoding="utf-8")
    updated = splice(page, render(rows))
    if updated == page:
        print("no change", file=sys.stderr)
        return 0

    PAGE.write_text(updated, encoding="utf-8")
    print(f"updated {PAGE.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
