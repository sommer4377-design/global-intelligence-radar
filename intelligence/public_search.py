"""Bounded public discovery adapters. Search hits are never evidence by themselves."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
import json
import re
from subprocess import run, TimeoutExpired
from threading import Lock
from urllib.parse import urlencode

from .blind_evidence import CAPITAL, action_set, host_of

HN = "https://hn.algolia.com/api/v1/search"
FED = "https://www.federalregister.gov/api/v1/documents.json"
CROSSREF = "https://api.crossref.org/works"
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
IGNORE = {"the", "this", "from", "after", "with", "about", "that", "president",
          "comment", "meeting", "before", "news", "some", "first", "global", "have", "what",
          "para", "sobre", "entre", "across", "their", "with", "they"}
CJK = re.compile(r"[\u3400-\u9fff]")
TERM = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][\w'-]{2,}|[\u3400-\u9fff]{2,8}|\b[A-Z]+[- ]?\d+(?:\.\d+)?\b")
DOCUMENT = re.compile(r"\b(?:[A-Z]{2,6}[- ]?\d{2,}|\d{4}-\d{4,})\b")
CROSSREF_SLOT = Lock()
LANG = {"English": "en", "Spanish": "es", "Portuguese": "pt", "Chinese": "zh"}


def queries(row, translator):
    title = row["title"]
    proper = [term for term in CAPITAL.findall(title) if term.casefold() not in IGNORE]
    ids = DOCUMENT.findall(title)
    raw = [term for term in TERM.findall(title) if term.casefold() not in IGNORE]
    action = next(iter(sorted(action_set(title))), "")
    salient = list(dict.fromkeys(ids + proper + sorted(raw, key=len, reverse=True)))
    primary = " ".join(salient[:2])[:78]
    alternate = " ".join(salient[:1] + ([action] if action else salient[2:3]))[:78]
    localized = translator(title, row["language"]) if row["language"] != "zh" else None
    translated_terms = TERM.findall(localized or "")
    chinese = " ".join(term for term in translated_terms if CJK.search(term))[:24]
    return list(dict.fromkeys(term for term in (primary, alternate, chinese) if len(term) >= 4))


def public_json(endpoint, parameters, timeout=7):
    target = endpoint + "?" + urlencode(parameters)
    try:
        proc = run(["curl", "-qfsS", "--proto", "=https", "--max-time", str(timeout),
                    "--max-filesize", "1500000",
                    "--write-out", "\n%{http_code}", target],
                   capture_output=True, timeout=timeout + 2)
        body, _, raw_code = proc.stdout.rpartition(b"\n")
        code = raw_code.decode("ascii", "replace")
        if proc.returncode or code != "200":
            return None, f"HTTP {code or '?'} / curl {proc.returncode}"
        data = json.loads(body)
        return (data, "") if isinstance(data, dict) else (None, "响应结构不符")
    except (OSError, ValueError, TimeoutExpired) as error:
        return None, type(error).__name__


def known_hit(title, url, provider, language=None, published=None):
    if not isinstance(url, str) or not url.startswith("https://") or not title:
        return None
    return dict(title=unescape(str(title))[:260], url=url, provider=provider,
                language=language, published=published)


def query_provider(provider, term, start, end):
    if provider == "HN":
        data, reason = public_json(HN, dict(
            query=term, tags="story", hitsPerPage=6,
            numericFilters=f"created_at_i>={int(start.timestamp())},created_at_i<={int(end.timestamp())}"))
        rows = (data or {}).get("hits", [])
        hits = [known_hit(row.get("title"), row.get("url"), provider, "en") for row in rows]
    elif provider == "Federal Register":
        data, reason = public_json(FED, {
            "conditions[term]": term, "per_page": 4,
            "conditions[publication_date][gte]": start.date().isoformat(),
            "conditions[publication_date][lte]": end.date().isoformat(),
        })
        rows = (data or {}).get("results", [])
        hits = [known_hit(row.get("title"), row.get("html_url"), provider, "en",
                          row.get("publication_date")) for row in rows]
    elif provider == "Crossref":
        with CROSSREF_SLOT:
            data, reason = public_json(CROSSREF, {
                "query.bibliographic": term, "rows": 4,
                "filter": f"from-pub-date:{start.date()},until-pub-date:{end.date()}",
            })
        rows = (data or {}).get("message", {}).get("items", [])
        hits = [known_hit((row.get("title") or [""])[0], row.get("URL"), provider) for row in rows]
    else:
        data, reason = public_json(GDELT, {
            "query": term, "mode": "artlist", "format": "json", "maxrecords": 6,
            "startdatetime": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
        }, timeout=8)
        rows = (data or {}).get("articles", [])
        hits = [known_hit(row.get("title"), row.get("url"), provider, LANG.get(row.get("language")))
                for row in rows]
    return [hit for hit in hits if hit], reason


def providers(row):
    text = row["title"].lower()
    if re.search(r"research|study|arxiv|paper|investiga|pesquisa|研究|论文", text):
        return ("Crossref", "HN")
    if row["lane"] == "全球重大事件" or "regulation" in action_set(text):
        return ("Federal Register", "HN")
    return ("HN",)


def federate(row, variants, start, end, allow_gdelt=False):
    if not variants:
        return [], [], False
    trails, hits = [], []
    chosen = (("GDELT",) if allow_gdelt else ()) + providers(row)
    for index, provider in enumerate(chosen[:2]):
        term = variants[min(index, len(variants)-1)] if provider == "HN" else variants[0]
        relevant, error = query_provider(provider, term, start, end)
        hits.extend(relevant)
        trails.append(dict(provider=provider, query=term, count=len(relevant), error=error))
    return hits, trails, len(chosen) > 2
