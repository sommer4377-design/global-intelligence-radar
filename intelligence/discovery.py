"""Read-only discovery using the existing feed registry and bounded follow-up."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import re
from urllib.parse import urlsplit

from automatic import timestamp
from news_web import fetch
from sources import REGISTRY

LOCAL_REGISTRY = REGISTRY + (
    ("Africanews", "全球重大事件", "https://www.africanews.com/feed/rss",
     "非洲", "en", "媒体", "非洲政治、经济和国际事务"),
    ("France 24 English", "全球重大事件", "https://www.france24.com/en/rss",
     "欧洲、全球", "en", "媒体", "国际政治与全球经济"),
)


def collect(registry=LOCAL_REGISTRY):
    with ThreadPoolExecutor(max_workers=6) as workers:
        return list(workers.map(lambda record: fetch(record[:3], limit=12), registry))


def focus(rows, starts, ends, *, exclude=()):
    """Read both feed lanes. Recheck leading global headlines independently of pre-scores."""
    feeds = {record[0]: record for record in LOCAL_REGISTRY}
    recent = {}
    for name, articles, _ in rows:
        record = feeds.get(name)
        if not record:
            continue
        lane = "全球重大事件" if record[1] == "全球重大事件" else "科技与产业"
        if record[5] in {"社区", "产品目录"}:
            continue
        in_window = 0
        for row in articles:
            try:
                stamp = timestamp(row["published"])
            except ValueError:
                continue
            if starts <= stamp < ends and row["url"] not in exclude:
                recent[row["url"]] = dict(row, lane=lane, language=record[4],
                                          headline_check=lane == "全球重大事件" and in_window < 3)
                in_window += 1
    return list(recent.values())


def origin_from_attribution(value):
    match = re.search(r"\b(Associated Press|Reuters|Agence France.Presse|EFE)\b", value, re.I)
    if not match:
        return ""
    return match[1].casefold().replace(" ", "-")


def match_event(text, rules):
    """Case rules require an action identifier as well as a named subject, place or product."""
    hits = [key for key, signatures in rules.items() if any(
        all(re.search(part, text, re.I) for part in signature) for signature in signatures)]
    return hits[0] if len(hits) == 1 else ""


def evidence_url(url):
    """Only public HTTPS references may enter the evidence register."""
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password else ""
