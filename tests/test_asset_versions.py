"""
Guards against the shared-asset `?v=N` drift called out in TECH_DEBT.md:
there's no build step in this repo, so every `<script src="...">`/`<link
href="...">` tag pointing at a `/static/...` file carries its own hand-typed
`?v=N`, and `static/sw.js`'s PRECACHE_URLS list carries a third copy of the
same number for the five (well, seven) pages it precaches for offline use.
Nothing enforced any of these staying in sync -- hit twice in one session
(2026-09-01): a shared file bumped in index.html but left stale in
admin.html/help.html/tokens.html/report.html (and vice versa), which never
broke online (StaticFiles ignores the query string -- it's the same file
underneath either way) but silently meant PRECACHE_URLS stopped matching
the *actual* request a drifted page made, so that page's real asset never
got cached for offline use. Invisible until specifically testing offline
mode, which is what surfaced both incidents.

Two checks, both failing loudly (with every offending file/version) rather
than silently:

1. Every top-level HTML page references each shared `/static/...` asset at
   the SAME version every other page does. A page-specific asset only one
   page loads has nothing to disagree with and is fine.
2. Every asset referenced by one of the pages `static/sw.js` actually
   precaches (PRECACHE_URLS) matches the version PRECACHE_URLS itself
   lists for that asset -- the exact failure mode from 2026-09-01.
"""

import re
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Every standalone top-level HTML page in the repo (not fragments/partials).
HTML_FILES = sorted(p.name for p in BASE_DIR.glob("*.html"))

# Route -> HTML file, for the pages static/sw.js's PRECACHE_URLS lists by
# route rather than filename (main.py's _serve_html routes -- see its own
# comments for why each of these, and only these, are precached).
PRECACHED_ROUTE_TO_FILE = {
    "/": "index.html",
    "/admin": "admin.html",
    "/help": "help.html",
    "/tokens": "tokens.html",
    "/report": "report.html",
    "/incidents": "incidents.html",
    "/assignments": "assignments.html",
}

# Matches src="/static/js/foo.js?v=3" or href="/static/app.css?v=38" (also
# vendor assets under /static/vendor/...).
ASSET_TAG_RE = re.compile(r'(?:src|href)="(/static/[^"?]+)\?v=(\d+)"')

# static/sw.js's PRECACHE_URLS is a plain JS array of single-quoted string
# literals, not HTML tags -- e.g. '/static/app.css?v=38',
SW_ASSET_RE = re.compile(r"'(/static/[^'?]+)\?v=(\d+)'")


def _asset_refs(text: str) -> dict:
    """path -> set of version strings referenced in this text."""
    refs = defaultdict(set)
    for path, version in ASSET_TAG_RE.findall(text):
        refs[path].add(version)
    return refs


def test_shared_assets_use_the_same_version_on_every_page():
    per_file_refs = {name: _asset_refs((BASE_DIR / name).read_text(encoding="utf-8")) for name in HTML_FILES}

    versions_by_asset = defaultdict(dict)  # asset path -> {version: [files]}
    for name, refs in per_file_refs.items():
        for path, versions in refs.items():
            for v in versions:
                versions_by_asset[path].setdefault(v, []).append(name)

    mismatches = {path: by_version for path, by_version in versions_by_asset.items() if len(by_version) > 1}
    assert not mismatches, (
        "Shared static assets are referenced at different ?v=N on different pages "
        "(same underlying file, so this won't break online loading, but it silently "
        "desyncs static/sw.js's offline precache -- bump every reference to match):\n"
        + "\n".join(
            f"  {path}: " + ", ".join(f"v{v} in {sorted(files)}" for v, files in sorted(by_version.items()))
            for path, by_version in sorted(mismatches.items())
        )
    )


def test_precached_pages_match_sw_js_precache_urls():
    sw_text = (BASE_DIR / "static" / "sw.js").read_text(encoding="utf-8")
    precache_versions = {path: version for path, version in SW_ASSET_RE.findall(sw_text)}

    errors = []
    for route, filename in PRECACHED_ROUTE_TO_FILE.items():
        html_text = (BASE_DIR / filename).read_text(encoding="utf-8")
        for path, version in ASSET_TAG_RE.findall(html_text):
            expected = precache_versions.get(path)
            if expected is None:
                errors.append(
                    f"{filename} (route {route!r}) loads {path}?v={version}, "
                    f"but static/sw.js's PRECACHE_URLS doesn't list {path} at all"
                )
            elif expected != version:
                errors.append(
                    f"{filename} (route {route!r}) loads {path}?v={version}, "
                    f"but static/sw.js's PRECACHE_URLS has {path}?v={expected}"
                )

    assert not errors, (
        "static/sw.js's PRECACHE_URLS is out of sync with a precached page's actual "
        "asset requests -- the precache silently stops covering that request until "
        "both are bumped to match:\n" + "\n".join(f"  {e}" for e in errors)
    )
