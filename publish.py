"""发布人工核对过的双语新闻快照。"""
from html import escape
import json

from radar import ROOT

CATEGORIES = ("全球重大事件", "华为全产业", "华为关联友商", "全球 AI", "全球科技商业")


def link(url, text):
    return (f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">'
            f'{escape(text)}</a>')


def card(row, number):
    return f"""
    <article id="news-{number}">
      <div class="meta">{escape(row['meta'])} · 原文{row['language']} · {row['priority']}</div>
      <h3>{escape(row['title'])}</h3>
      <p><strong>事件概述</strong>{escape(row['overview'])}</p>
      <p><strong>为什么值得关注</strong>{escape(row['why'])}</p>
      <details><summary>查看原始语言</summary>
        <p>{escape(row['original_title'])}</p><p>{escape(row['original_summary'])}</p>
      </details>
      {link(row['reading_url'], '阅读原文 ↗')}
    </article>"""


def main():
    snapshot = json.loads((ROOT / "editorial_snapshot.json").read_text())
    rows = list(enumerate(snapshot["rows"], 1))
    ranked = sorted(rows, key=lambda entry: entry[1]["priority"])
    featured = [entry for entry in ranked if entry[1]["priority"] == "P0"]
    featured += [entry for entry in ranked if entry[1]["priority"] == "P1"
                 and entry[1]["category"] in ("华为关联友商", "华为全产业")][:2]
    highlights = "".join(f'<li><a href="#news-{n}">{escape(row["title"])}</a></li>'
                         for n, row in featured)
    sections = []
    for category in CATEGORIES:
        items = [(n, row) for n, row in ranked
                 if row["category"] == category and row["priority"] != "归档"]
        sections.append(f'<section><h2>{category} <small>{len(items)} 条</small></h2>'
                        f'<div class="cards">{"".join(card(row, n) for n, row in items)}</div></section>')
    archive = [(n, row) for n, row in rows if row["priority"] == "归档"]
    audit = []
    for n, row in rows:
        raw = row["original_url"]
        https = row["reading_url"]
        codes = row["checks"]
        upgrade = (f' · {link(https, "HTTPS 入口")}（{codes[https]["status"]}）'
                   if raw != https else "")
        audit.append(f'<li>{n}. {escape(row["title"])} · '
                     f'{link(raw, "采集时的原文地址")}（{codes[raw]["status"]}）{upgrade}</li>')
    demo = json.loads((ROOT / "sample.json").read_text())
    demos = "".join(f'<li>{escape(item["title"])}</li>' for item in demo)
    css = (ROOT / "news.css").read_text()
    latest = sorted(ROOT.glob("brief-????-??-??-??.html"), reverse=True)[:2]
    reports = "".join(f'<a href="{item.name}">{item.stem.removeprefix("brief-")[:-3]} '
                      f'{"早报" if item.stem.endswith("-08") else "晚报"}</a>' for item in sorted(latest))
    output = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Intelligence Radar</title><style>{css}</style>
<header><h1>Global Intelligence Radar</h1><p>全球新闻与科技商业情报 · 2026-11-21 新闻快照</p>
<nav>{reports}<a href="#today">今日重点</a><a href="#news">五大栏目</a><a href="#audit">原文核查</a><a href="#demo">演示数据</a></nav></header>
<main>
<section class="lead" id="today"><h2>今日重点</h2><ol>{highlights}</ol></section>
<div id="news"><p>简体中文内容依据来源摘要编写。P0 为跨境或系统性影响突出，P1 为影响明确，P2 为行业动态与背景。</p>
{"".join(sections)}</div>
<details><summary>归档资讯 · {len(archive)} 条</summary>
<p>地方服务资讯、缺乏明确影响证据的灾害通报等留在归档。</p>
<div class="cards">{"".join(card(row, n) for n, row in archive)}</div></details>
<details class="audit" id="audit"><summary>原文链接核查 · {len(rows)} 条</summary>
<p>核查时间 {snapshot['audited_at']}。使用 GET 请求并跟随重定向，HTTPS 保持证书校验。</p>
<ol style="list-style:none;padding:0">{"".join(audit)}</ol></details>
<details id="demo"><summary>演示数据 · {len(demo)} 条合成样本</summary>
<p>以下为版式备份使用的虚构示例。</p><ul>{demos}</ul></details>
</main></html>"""
    (ROOT / "editorial-archive.html").write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
