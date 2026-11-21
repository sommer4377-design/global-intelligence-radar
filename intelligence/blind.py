"""Run on unseen RSS records without event-specific labels or evidence fixtures."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html import escape
import json
from pathlib import Path
import resource
import sys
import time
from urllib.parse import urlsplit

from automatic import Translator, timestamp
from radar import BEIJING
from datetime import date, datetime, time as clock_time
from .blind_evidence import (
    REGION, action_set, article, compare, host_of, related, translation_cache,
)
from .discovery import LOCAL_REGISTRY, collect, focus
from .page import STYLE

ARTICLE_LIMIT = 40


def event_groups(rows, translator):
    groups = []
    for row in sorted(rows, key=lambda item: (item["headline_check"], bool(action_set(item["title"]))),
                      reverse=True):
        matches = [group for group in groups if all(
            abs(timestamp(row["published"]) - timestamp(peer["published"])) <= timedelta(hours=40)
            and related(row, peer, translator) >= .38 for peer in group)]
        if len(matches) == 1:
            matches[0].append(row)
        else:
            groups.append([row])
    return groups


def significance(group, verified):
    """Rank observed scope, action and material detail without brand or category bonuses."""
    words = " ".join(item["wording"] for claim in verified for item in claim["statements"])
    actions = {action for claim in verified for action in claim["action"]}
    scope = 4 if REGION.search(words) else 2 if words else 0
    severity = 3 if actions & {"security", "regulation", "disaster"} else 2 if actions else 0
    substance = min(3, len(actions))
    freshness = 2 if words else 0
    durability = int(bool(actions & {"agreement", "acquisition", "regulation"}))
    return dict(scope=scope, severity=severity, substance=substance,
                freshness=freshness, durability=durability)


def run_window(day, snapshot, translator):
    registry = {item[0]: item for item in LOCAL_REGISTRY}
    start = datetime.combine(day, clock_time.min, BEIJING)
    end = start + timedelta(days=1)
    raw = snapshot["results"]
    in_window = focus(raw, start, end)
    searchable = [dict(row, role=registry[row["source"]][5])
                  for row in focus(raw, start-timedelta(days=1), end)]
    leads = [dict(row, role=registry[row["source"]][5]) for row in in_window]
    domains = {host_of(row["url"]) for row in searchable if "." in host_of(row["url"])}
    groups = event_groups(leads, translator)
    candidates = []
    fetches = {}
    for group in groups:
        representative = group[0]
        exact = {row["url"] for row in group}
        ranked = sorted(((related(representative, row, translator), row)
                         for row in searchable if row["url"] not in exact),
                        key=lambda pair: pair[0], reverse=True)
        extras = [row for score, row in ranked if score >= .29][:4]
        selected = list({row["url"]: row for row in group + extras}.values())[:5]
        candidates.append((group, selected))
        for row in selected:
            if len(fetches) < ARTICLE_LIMIT:
                fetches.setdefault(row["url"], row)
    with ThreadPoolExecutor(max_workers=6) as workers:
        fetched = dict(zip(fetches, workers.map(lambda row: article(row, domains), fetches.values())))
    search_by_url = {row["url"]: row for row in searchable}
    # Follow links that refer back to an indexed reliable publication in the same bounded window.
    next_fetches = {}
    for _, selected in candidates:
        for row in tuple(selected):
            report, _ = fetched.get(row["url"], (None, ""))
            if report:
                for link in report.links:
                    found = search_by_url.get(link)
                    if found and found not in selected and related(row, found, translator) >= .29 and len(selected) < 5:
                        selected.append(found)
                        if link not in fetched and len(fetched) + len(next_fetches) < ARTICLE_LIMIT:
                            next_fetches.setdefault(link, found)
    with ThreadPoolExecutor(max_workers=4) as workers:
        fetched.update(zip(next_fetches, workers.map(lambda row: article(row, domains), next_fetches.values())))
    records = []
    for group, selected in candidates:
        sources = [fetched.get(row["url"], (None, "达到正文读取上限")) for row in selected]
        reports = [report for report, _ in sources if report]
        documents = []
        pair_notes = []
        for i, first in enumerate(reports):
            for second in reports[i+1:]:
                if host_of(first.row["url"]) == host_of(second.row["url"]):
                    continue
                claims, reason = compare(first, second, translator)
                if claims:
                    documents.extend(claims)
                elif reason:
                    pair_notes.append(reason)
        if any("核心冲突" in reason for reason in pair_notes):
            documents = []
        reason = (
            "缺少可通过语义与独立阅读验收的通用中文生成器" if documents else
            (pair_notes[0] if pair_notes else "未找到第二份可读的独立原文")
        )
        records.append(dict(
            title=group[0]["title"], links=[row["url"] for row in group], merged=len(group),
            headline_check=any(row["headline_check"] for row in group), reason=reason,
            extracted_candidates=len(selected),
            evidence=[dict(source=row["source"], url=row["url"],
                           origin=report.lineage if report else error)
                      for row, (report, error) in zip(selected, sources)],
            verified_pairs=len(documents),
            claim_matches=[dict(subject=doc["subject"], action=doc["action"],
                                evidence=[{key: value for key, value in atom.items() if key != "wording"}
                                          for atom in doc["statements"]]) for doc in documents],
            score=significance(group, documents),
        ))
    records.sort(key=lambda row: (sum(row["score"].values()), row["headline_check"]), reverse=True)
    return dict(
        date=day.isoformat(), observed=snapshot.get("observed"), leads=len(leads),
        headline_count=sum(row["headline_check"] for row in leads),
        identified=sum(bool(action_set(row["title"])) for row in leads),
        candidate_groups=len(records), merged=sum(row["merged"]-1 for row in records),
        retrieved=sum(report is not None for report, _ in fetched.values()),
        fetched=len(fetched), checked=sum(row["extracted_candidates"] >= 2 for row in records),
        verified=sum(row["verified_pairs"] > 0 for row in records),
        written=0, source_count=len({row["source"] for row in leads}), records=records,
        feed_ok=sum(bool(rows) for _, rows, _ in raw), feed_total=len(raw),
    )


def render(windows, path):
    sections = []
    for window in windows:
        rows = "".join(
            f'<details><summary>{escape(item["title"])}</summary>'
            f'<p>{escape(item["reason"])}</p><ul>'
            + "".join(f'<li><a href="{escape(evidence["url"], quote=True)}">'
                      f'{escape(evidence["source"])}</a> · {escape(evidence["origin"])}</li>'
                      for evidence in item["evidence"]
                      if urlsplit(evidence["url"]).scheme == "https")
            + '</ul></details>' for item in window["records"]
        )
        sections.append(f'<section><h2>{window["date"]} · 盲测窗口</h2>'
                        f'<p>识别动作 {window["identified"]} 条 · 候选 {window["candidate_groups"]} 组 · 通过双源断言 {window["verified"]} 组 · '
                        f'出报 {window["written"]} 篇</p>{rows}</section>')
    path.write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Global Intelligence Radar · 盲测</title>'
        f'<style>{STYLE}details{{padding:.5rem 0;border-bottom:1px solid #d7ded6}}</style>'
        '<main><header><h1>Global Intelligence Radar</h1><p>本地盲测 · 北京时间</p></header>'
        '<section><h2>全球焦点</h2><p>本次没有同时通过自动核实和中文写作验收的稿件。</p></section>'
        + "".join(sections) + '</main>', encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", nargs=2, action="append", metavar=("DAY", "RSS_JSON"), required=True)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--collect", action="store_true",
                        help="Record fresh RSS for each window before processing, without overwriting.")
    args = parser.parse_args()
    started = time.monotonic()
    translator = translation_cache(Translator(args.models).translate)
    results = []
    for stamp, capture in args.window:
        path = Path(capture)
        if args.collect:
            with path.open("x", encoding="utf-8") as output:
                json.dump(dict(observed=datetime.now(BEIJING).isoformat(), results=collect()), output,
                          ensure_ascii=False)
        capture_data = json.loads(path.read_text())
        results.append(run_window(date.fromisoformat(stamp), capture_data, translator))
    args.output.mkdir(parents=True, exist_ok=True)
    render(results, args.output / "index.html")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    metrics = dict(seconds=round(time.monotonic()-started, 2),
                   peak_mib=round(rss / (2**20 if sys.platform == "darwin" else 1024), 1),
                   windows=results)
    (args.output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"盲测完成，耗时 {metrics['seconds']} 秒，峰值 {metrics['peak_mib']} MiB。")


if __name__ == "__main__":
    main()
