"""Measure discovery, source lineage and claim agreement on a blind local sample."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape
import json
from pathlib import Path
import re
import resource
import sys
import time
from urllib.parse import urlsplit

from automatic import Translator, canonical
from radar import BEIJING
from .blind import event_groups
from .blind_evidence import (
    REGION, action_set, article, compare, extract_claims, host_of, related, translation_cache,
)
from .discovery import LOCAL_REGISTRY, collect, focus
from .page import STYLE
from .gkg_index import read_archive, shortlist
from .public_search import federate, queries

MAX_CANDIDATES = 32
MAX_ARTICLES = 3
SOURCE_REGISTRY = {record[0]: record for record in LOCAL_REGISTRY}
EXTRA_DOMAINS = {
    "apnews.com": ("AP", "媒体"), "reuters.com": ("Reuters", "媒体"),
    "bbc.com": ("BBC", "媒体"), "federalregister.gov": ("Federal Register", "机构"),
    "govinfo.gov": ("GovInfo", "机构"), "sec.gov": ("SEC", "机构"),
    "arxiv.org": ("arXiv", "研究索引"), "ec.europa.eu": ("欧洲委员会", "机构"),
    "consilium.europa.eu": ("欧盟理事会", "机构"),
}


def is_https(url):
    return urlsplit(url).scheme == "https"


def build_catalog(rows):
    index = dict(EXTRA_DOMAINS)
    for row in rows:
        host = host_of(row["url"])
        if host:
            index[host] = (row["source"], row["role"])
    return index


def priority(row):
    actions = action_set(row["title"])
    # Prioritize evident consequences and confirmed actions, without issuer bonuses.
    return (bool(actions & {"security", "regulation", "agreement", "financial"}),
            bool(REGION.search(row["title"])),
            bool(row["headline_check"]), bool(actions))


def distinct(rows, exclude=()):
    result = {}
    for row in rows:
        key = canonical(row["url"])
        if key not in exclude:
            role = SOURCE_REGISTRY[row["source"]][5]
            result.setdefault(key, dict(row, role=role))
    return list(result.values())


def examine(group, index, all_rows, translator, start, end, trial_gdelt, archive):
    opened = time.monotonic()
    seed = group[0]
    seeds = {canonical(row["url"]) for row in group}
    variants = queries(seed, translator)
    hits, searches, _ = federate(seed, variants, start-timedelta(days=2), end, trial_gdelt)
    rows = {canonical(row["url"]): row for row in group}
    for row in all_rows:
        if canonical(row["url"]) not in rows and related(seed, row, translator) >= .32:
            rows[canonical(row["url"])] = row
    for hit in hits:
        name, role = index.get(host_of(hit["url"]), ("", ""))
        if not name or not hit["language"]:
            continue
        row = dict(hit, source=name, role=role, published=hit.get("published") or "未核实发布时间")
        if related(seed, row, translator) >= .30:
            rows.setdefault(canonical(row["url"]), row)
    archive_matches = shortlist(seed, archive, translator)
    if archive:
        searches.append(dict(provider="GDELT GKG", query="跨来源原始标题索引", count=len(archive_matches), error=""))
    for row in archive_matches:
        key = canonical(row["url"])
        rows.setdefault(key, row)
    candidates = sorted(rows.values(), key=lambda row: (
        canonical(row["url"]) in seeds, row["role"] in {"官方", "机构"},
        related(seed, row, translator)), reverse=True)
    selected = []
    by_host = set()
    for row in candidates:
        if host_of(row["url"]) not in by_host:
            by_host.add(host_of(row["url"]))
            selected.append(row)
        if len(selected) == MAX_ARTICLES:
            break
    documents = []
    fetched = []
    errors = []
    matched = []
    for row in selected:
        report, error = article(row, index.keys() | {host_of(row["url"]) for row in archive_matches})
        fetched.append(dict(url=row["url"], source=row["source"],
                            disposition=report.lineage if report else error))
        if report is None:
            continue
        for prior in documents:
            claims, reason = compare(prior, report, translator)
            if claims:
                matched.extend(claims)
            elif reason:
                errors.append(reason)
        documents.append(report)
        if matched:
            break
    # Keep exact claim snippets internal. The public-facing metrics store facts with source IDs.
    packet = [{"subject":claim["subject"], "action":claim["action"],
               "evidence":claim["statements"]} for claim in matched]
    independently_sourced = any(
        first.origin and second.origin and first.origin != second.origin
        and "转载或转述" not in (first.lineage, second.lineage)
        for i, first in enumerate(documents) for second in documents[i+1:])
    conflict = any("核心冲突" in error for error in errors)
    passed = bool(matched) and not conflict
    reason = ("核心断言冲突" if conflict else
              "已核实，待语义与写作测试" if passed else
              errors[0] if errors else
              "已取得两篇正文，但未能确认同一核心断言" if len(documents) >= 2 else
              "未取得第二份相关原文")
    return dict(
        title=seed["title"], url=seed["url"], source=seed["source"], language=seed["language"],
        candidate_links=len(selected), readable=len(documents), independent=independently_sourced,
        verified=passed, reason=reason, terms=variants, searches=searches, sources=fetched,
        seconds=round(time.monotonic()-opened, 2),
        packet=packet if passed else [],
        individual_source_atoms=sum(len(extract_claims(report)) for report in documents),
        verified_claims=[dict(subject=claim["subject"], action=claim["action"],
                              evidence=[{k:v for k,v in atom.items() if k != "wording"}
                                        for atom in claim["statements"]]) for claim in (matched if passed else [])],
    )


def render(report, output):
    articles = []
    for row in report["records"]:
        status = "核心断言已核实" if row["verified"] else "暂缓"
        links = " · ".join(f'<a href="{escape(item["url"], quote=True)}">{escape(item["source"])}</a>'
                           for item in row["sources"] if is_https(item["url"]))
        trail = "、".join(f"{call['provider']} {call['count']}" for call in row["searches"])
        articles.append(
            f'<details><summary>{escape(row["title"])}</summary>'
            f'<p>{status} · {escape(row["reason"])} · {row["seconds"]} 秒</p>'
            f'<p>{escape(trail)} · {links}</p></details>')
    html = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Global Intelligence Radar · 独立证据验证</title>'
        f'<style>{STYLE}details{{padding:.5rem 0;border-bottom:1px solid #d7ded6}}</style>'
        '<main><header><h1>Global Intelligence Radar</h1><p>本地技术验证 · 非正式简报</p></header>'
        '<section><h2>全球焦点</h2><p>本页仅展示本轮证据核实状态。通过全部新闻写作与事实校验的稿件才可进入正式简报。</p></section>'
        f'<section><h2>证据检索结果</h2><p>{report["candidate_count"]} 个候选，'
        f'{report["verified"]} 个通过核心事实核实。</p>{"".join(articles)}</section></main>')
    (output / "index.html").write_text(html, encoding="utf-8")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("feed", "output"):
        cli.add_argument("--"+name, type=Path, required=True)
    for name in ("start", "end"):
        cli.add_argument("--"+name, type=datetime.fromisoformat, required=True)
    cli.add_argument("--models", type=Path)
    cli.add_argument("--collect", action="store_true")
    cli.add_argument("--exclude-report", type=Path)
    cli.add_argument("--gkg", action="append", type=Path, default=[])
    cli.add_argument("--packets", type=Path)
    args = cli.parse_args()
    started = time.monotonic()
    if not args.start.utcoffset() or not args.end.utcoffset() or args.end <= args.start:
        cli.error("请使用含北京时间偏移的有序时间窗口。")
    if args.collect:
        with args.feed.open("x", encoding="utf-8") as capture:
            results = collect()
            json.dump(dict(observed=datetime.now(BEIJING).isoformat(), results=results), capture,
                      ensure_ascii=False)
    raw = json.loads(args.feed.read_text())
    exclude = set()
    if args.exclude_report:
        exclude = {canonical(url) for url in re.findall(r"https?://[^\s)]+", args.exclude_report.read_text())}
    translator = translation_cache(Translator(args.models).translate)
    recent = distinct(focus(raw["results"], args.start, args.end), exclude)
    index_rows = distinct(focus(raw["results"], args.start-timedelta(days=2), args.end), exclude)
    registry = build_catalog(index_rows)
    if len(args.gkg) > 2:
        cli.error("每轮最多读取两个 GKG 切片。")
    archive = [row for file in args.gkg for row in read_archive(file, args.end)]
    if args.exclude_report:
        recent = [row for row in recent if not re.search(r"COP31|(?:earthquake|地震).*6\.7.*(?:Peru|秘鲁)",
                                                         row["title"], re.I)]
    groups = sorted(event_groups(recent, translator), key=lambda g: priority(g[0]), reverse=True)[:MAX_CANDIDATES]
    results = []
    if groups:
        # Try the broad index once. It does not gate the other providers.
        results.append(examine(groups[0], registry, index_rows, translator, args.start, args.end, True, archive))
        # Crossref public pool allows one in-flight query. Serialize only its workflow-level plans.
        # At most two calls and three distinct article hosts per candidate.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results.extend(pool.map(lambda g: examine(g, registry, index_rows, translator,
                                                       args.start, args.end, False, archive), groups[1:]))
    packs = [dict(title=row["title"], url=row["url"], claims=row.pop("packet")) for row in results]
    if args.packets:
        args.packets.write_text(json.dumps(packs, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = dict(
        observed=raw.get("observed"), start=args.start.isoformat(), end=args.end.isoformat(),
        candidate_count=len(results), undispatched=len(recent)-sum(len(group) for group in groups),
        feeds_ok=sum(bool(rows) for _, rows, _ in raw["results"]), feeds_total=len(raw["results"]),
        feed_rows=sum(len(rows) for _, rows, _ in raw["results"]),
        second_link=sum(row["candidate_links"] >= 2 for row in results),
        second_readable=sum(row["readable"] >= 2 for row in results),
        independent=sum(row["independent"] for row in results), verified=sum(row["verified"] for row in results),
        gkg_articles=len(archive), gkg_trusted_hosts=len({host_of(row["url"]) for row in archive if row["source"]}),
        total_seconds=round(time.monotonic()-started, 2),
        peak_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss /
                       (2**20 if sys.platform == "darwin" else 1024), 1),
        api_calls=dict(Counter(call["provider"] for row in results for call in row["searches"])),
        records=results)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    render(metrics, args.output)
    print({key:value for key,value in metrics.items() if key != "records"})


if __name__ == "__main__":
    main()
