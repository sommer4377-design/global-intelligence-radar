"""从免费公开源生成可离线打开的新闻网页。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import escape, unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit
from subprocess import run, TimeoutExpired
import xml.etree.ElementTree as ET

from radar import BEIJING, ROOT

GLOBAL, HUAWEI, AI = "全球重大事件", "华为", "AI 资讯"
SOURCES = (
    ("联合国新闻", GLOBAL, "https://news.un.org/feed/subscribe/en/news/all/rss.xml"),
    ("Agência Brasil", GLOBAL, "https://agenciabrasil.ebc.com.br/rss/geral/feed.xml"),
    ("GDACS 全球灾害警报", GLOBAL, "https://www.gdacs.org/xml/rss.xml"),
    ("华为媒体中心", HUAWEI, "https://www.huawei.com/en/media-center"),
    ("OpenAI News", AI, "https://openai.com/news/rss.xml"),
    ("Google AI", AI, "https://blog.google/innovation-and-ai/technology/ai/rss/"),
)


def read(url):
    try:
        result = run(
            ["curl", "--fail", "--silent", "--show-error", "--location",
             "--proto", "=https", "--proto-redir", "=https", "--compressed",
             "--connect-timeout", "6", "--max-time", "16", "--retry", "1", "--retry-max-time", "18",
             "--max-filesize", "2000000", "--user-agent", "Global-Intelligence-Radar/0.1", url],
            capture_output=True, timeout=36,
        )
    except TimeoutExpired as error:
        raise OSError("来源读取超时") from error
    if result.returncode:
        raise OSError(result.stderr.decode(errors="replace").strip())
    if len(result.stdout) > 2_000_000:
        raise ValueError("解压后来源超过 2 MB")
    return result.stdout


def plain(value):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def rss_items(data):
    root = ET.fromstring(data)
    atom = "{http://www.w3.org/2005/Atom}"
    if root.tag == atom + "feed":
        for item in root.findall(atom + "entry")[:12]:
            date = item.findtext(atom + "published") or item.findtext(atom + "updated", "")
            try:
                published = datetime.fromisoformat(date.replace("Z", "+00:00"))
            except ValueError:
                continue
            link = next((tag.get("href", "") for tag in item.findall(atom + "link")
                         if tag.get("rel", "alternate") == "alternate"), "")
            if published.tzinfo:
                yield (plain(item.findtext(atom + "title")), link,
                       published.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M 北京时间"),
                       plain(item.findtext(atom + "summary") or item.findtext(atom + "content")))
        return
    for item in root.findall("./channel/item")[:12]:
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
        except ValueError:
            continue
        if published.tzinfo is None:
            continue
        yield (plain(item.findtext("title")), item.findtext("link", "").strip(),
               published.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M 北京时间"),
               plain(item.findtext("description")))


class Description(HTMLParser):
    value = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "description":
            self.value = attributes.get("content", "")


def huawei_items(url):
    page = read(url).decode("utf-8", errors="replace")
    cards = re.findall(r'<a href="([^"]+)"[^>]*class="newsroom-box-(?:item|img)[^"]*"[^>]*>(.*?)</a>',
                       page, re.S)
    for link, card in cards[:6]:
        title = re.search(r"<h4[^>]*>(.*?)</h4>", card, re.S)
        date = re.search(r'<span class="newsroom-box-(?:item-)?time">(.*?)</span>', card, re.S)
        if not title or not date:
            continue
        published = datetime.strptime(plain(date[1]), "%b %d, %Y").date()
        link = urljoin(url, link)
        if urlsplit(link).hostname != "www.huawei.com":
            continue
        description = Description()
        try:
            description.feed(read(link).decode("utf-8", errors="replace"))
        except OSError as error:
            print(f"华为摘要读取失败：{error}")
        yield plain(title[1]), link, f"{published}（来源仅提供日期）", plain(description.value)


def fetch(source, limit=5):
    name, category, url = source
    try:
        items = huawei_items(url) if category == HUAWEI else rss_items(read(url))
        rows = []
        for title, link, published, summary in items:
            if urlsplit(link).scheme not in {"http", "https"} or not title:
                continue
            rows.append(dict(source=name, category=category, title=title, url=link,
                             published=published, summary=summary[:260] + ("…" if len(summary) > 260 else "")))
        return name, rows[:limit], "" if rows else "没有解析出带发布日期的条目"
    except (OSError, ValueError, ET.ParseError) as error:
        return name, [], str(error)


def cards(rows):
    result = []
    for row in rows:
        title, url, source, published, summary = (escape(row[key]) for key in
                                                  ("title", "url", "source", "published", "summary"))
        result.append(f'<article><div class="meta">{source} · {published}</div>'
                      f'<h3>{title}</h3><p>{summary or "来源未提供摘要。"}</p>'
                      f'<a href="{url}" target="_blank" rel="noopener noreferrer">阅读原文 ↗</a></article>')
    return "".join(result)


def main():
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(fetch, SOURCES))
    unique = {}
    status = []
    for name, rows, error in results:
        print(f"{name}：{error or str(len(rows)) + ' 条'}")
        unique.update({row["url"]: row for row in rows})
        status.append(f"{name}：{error or str(len(rows)) + ' 条'}")
    target = ROOT / "raw-news.html"
    if not unique:
        raise SystemExit("本次未取到真实新闻。已有网页快照保留。")
    demo = json.loads((ROOT / "sample.json").read_text())
    for row in demo:
        row["summary"] = "合成样本，仅用于查看版式。"
    now = datetime.now(BEIJING)
    blocks = []
    for category in (GLOBAL, HUAWEI, AI):
        rows = sorted((row for row in unique.values() if row["category"] == category),
                      key=lambda row: row["published"], reverse=True)
        blocks.append(f'<section><h2>{category} <small>{len(rows)} 条</small></h2>'
                      f'<div class="cards">{cards(rows) or "<p>本轮没有取得数据。</p>"}</div></section>')
    output = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Intelligence Radar</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f3f5f8;color:#182637;font:16px/1.6 -apple-system,BlinkMacSystemFont,system-ui,sans-serif}}
header{{background:#12243a;color:#fff;padding:3rem max(5vw,calc((100vw - 1100px)/2)) 2.6rem}}
h1{{font-size:clamp(2rem,6vw,3.6rem);line-height:1.1;margin:.2em 0;letter-spacing:-.04em}}
header p{{color:#c7d9e7;margin:.5em 0}} main{{max-width:1160px;margin:auto;padding:1.8rem 5vw 4rem}}
nav{{display:flex;gap:1.2rem;flex-wrap:wrap}} a{{color:#075e68}} header a{{color:#a8f0dc}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:1rem}}
article{{display:flex;flex-direction:column;background:white;border:1px solid #dce3e9;border-radius:12px;padding:1.1rem;overflow-wrap:anywhere}}
article h3{{margin:.45rem 0;font-size:1.1rem;line-height:1.35}} article p{{margin:.3rem 0 .9rem}} article a{{margin-top:auto}}
.meta,small{{color:#536678;font-size:.8rem}} section{{margin-top:2.2rem}} h2{{margin-bottom:.8rem}}
details{{margin:1.6rem 0;padding:1rem;border:1px solid #dce3e9;border-radius:12px}} summary{{cursor:pointer}}
#demo{{background:#fff6e5}} #demo article{{border-color:#ebd5a5}}
</style>
<header><h1>Global Intelligence Radar</h1><p>公开来源新闻快照 · {now:%Y-%m-%d %H:%M} 北京时间</p>
<nav><a href="#real">真实新闻</a><a href="#demo">演示数据</a></nav></header>
<main><div id="real"><h2>真实新闻 <small>{len(unique)} 条</small></h2>
<p>标题与摘要保留来源语言。摘要来自来源提供的简介，日期精度以原始来源为准。</p>
<details><summary>查看来源读取结果</summary><ul>{''.join(f"<li>{escape(line)}</li>" for line in status)}</ul></details>
{''.join(blocks)}</div>
<details id="demo"><summary>演示数据 · 合成样本</summary><p>以下内容仅用于预览，链接为占位链接。</p>
<div class="cards">{cards(demo)}</div></details></main></html>"""
    target.write_text(output, encoding="utf-8")
    print(f"已生成 {target.name}：{len(unique)} 条真实新闻")


if __name__ == "__main__":
    main()
