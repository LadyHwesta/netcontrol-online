#!/usr/bin/env python3
"""
UI Translation — Known-Strings Extractor
==========================================
Dev-maintenance script (not a runtime dependency). Regex-scans every
*.html file and static/js/*.js file for t('...') / t("...") call sites
and tn(n, '...', '...') call sites (static/js/i18n.js's translation-
lookup helpers -- tn() is the count-aware sibling of t(), translating
its singular/plural template arguments as two independent strings) and
writes the deduped list of English source strings to
static/i18n/known_strings.json.

That file is used only to seed an admin's "enable a language" bulk
pre-translation job (routers/translation.py's run_enable_language_job)
with a complete list of what to translate up front — it is NOT required
for correctness at runtime. The actual translation cache (translation_cache
table) self-heals on any string this manifest misses or hasn't caught up
with yet, the first time a real visitor renders it in a given language.

Re-run by hand after adding new t(...) call sites:

    python3 extract_i18n_strings.py

Also prints a rough per-file count, so it's easy to see extraction
coverage grow as more of the app is wrapped in t() over time (tracked in
TECH_DEBT.md).
"""

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
OUTPUT_PATH = ROOT / "static" / "i18n" / "known_strings.json"

# Matches t('...') / t("...") — the first argument only (the English
# source text); a second `, context` argument, if present, is ignored
# here since the manifest only needs the distinct source strings for
# pre-translation, not the disambiguation context. Also matches _t(...),
# aprs-map.js's local fallback alias for pages (public.html) that don't
# load i18n.js at all -- same call shape, still worth pre-warming wherever
# i18n.js IS loaded.
T_CALL_RE = re.compile(r"""\b_?t\(\s*(['"])((?:\\.|(?!\1).)*)\1""")

# Matches tn(n, '...', '...') — both the singular and plural template
# arguments (each its own independent t()-style cache key, containing a
# literal "{n}" placeholder substituted after translation).
TN_CALL_RE = re.compile(
    r"""\btn\(\s*[^,]+,\s*(['"])((?:\\.|(?!\1).)*)\1\s*,\s*(['"])((?:\\.|(?!\3).)*)\3"""
)

# Matches data-i18n="..." / data-i18n-placeholder="..." / data-i18n-title="..."
# -- the static-HTML equivalent of a t() call site (static/js/i18n.js's
# translatePage() applies t() to each of these at runtime).
DATA_I18N_RE = re.compile(r"""data-i18n(?:-placeholder|-title)?="([^"]*)\"""")


def _unescape(s: str) -> str:
    return s.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def extract_from_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found = [_unescape(m.group(2)) for m in T_CALL_RE.finditer(text)]
    for m in TN_CALL_RE.finditer(text):
        found.append(_unescape(m.group(2)))
        found.append(_unescape(m.group(4)))
    # data-i18n="..." values are raw HTML source -- the browser HTML-decodes
    # attribute values (&amp; -> &, etc.) before .dataset.i18n ever sees
    # them, and that decoded form is what actually becomes the t() cache
    # key at runtime, so decode here too or every string containing an
    # entity would silently never match its pre-translated cache entry.
    found += [html.unescape(m.group(1)) for m in DATA_I18N_RE.finditer(text)]
    return found


def main():
    files = sorted(ROOT.glob("*.html")) + sorted((ROOT / "static" / "js").glob("*.js"))
    seen: dict[str, None] = {}  # insertion-ordered dedupe
    per_file_counts = []

    for path in files:
        found = extract_from_file(path)
        if found:
            per_file_counts.append((path.relative_to(ROOT), len(found)))
        for s in found:
            seen.setdefault(s, None)

    strings = list(seen.keys())
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(strings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {len(strings)} unique string(s) to {OUTPUT_PATH.relative_to(ROOT)}")
    for rel_path, count in per_file_counts:
        print(f"  {count:4d}  {rel_path}")


if __name__ == "__main__":
    main()
