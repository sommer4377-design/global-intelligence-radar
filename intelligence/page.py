"""Render the SQLite edition records without scripts or remote assets."""
from html import escape
import json
from pathlib import Path
import re


STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f8f7f2;color:#202522;font:17px/1.8 system-ui,sans-serif}
main{max-width:55rem;padding:2rem 1.2rem 4rem;margin:auto}header{padding-bottom:1rem;border-bottom:2px solid #254639}
h1,h2,h3{line-height:1.3}h1{font-family:Georgia,serif;font-size:clamp(1.65rem,6vw,3rem);margin:0}
article{padding:1rem 0;border-bottom:1px solid #cbcfc7}.meta{font-size:.88rem;color:#4c5c51}
h3{margin:.25rem 0}summary,a{color:#244b3d}summary{cursor:pointer}p{margin:.65rem 0}
a,summary{overflow-wrap:anywhere}a:focus-visible,summary:focus-visible{outline:2px solid #177244}
"""


def build(db, output):
    output.mkdir(parents=True, exist_ok=True)
    editions = [row[0] for row in db.execute("SELECT DISTINCT edition FROM intel_decision ORDER BY edition DESC")]
    if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}-(08|20)", x) for x in editions):
        raise ValueError("Invalid edition identifier.")
    history = "".join(f'<li><a href="brief-{x}.html">{x[:10]} {"早报" if x.endswith("08") else "晚报"}</a></li>'
                      for x in editions)
    latest = None
    for edition in editions:
        data = [json.loads(doc) for doc, in db.execute(
            "SELECT document FROM intel_publication WHERE edition=?", (edition,))]
        data.sort(key=lambda item: item["rank"], reverse=True)
        parts = []
        for title, items in (("全球焦点 TOP 3", data[:3]),
                             ("重要动态", [x for x in data[3:] if x["rank"][0]]),
                             ("更多发现", [x for x in data[3:] if not x["rank"][0]])):
            empty = "<p>本轮未取得足够的已核实报道。采集和证据覆盖仍有限。</p>" if not items else ""
            parts.append(f'<section><h2>{title}</h2>{empty}{"".join(card(x) for x in items)}</section>')
        period = "早报" if edition.endswith("08") else "晚报"
        html = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width,initial-scale=1"><title>Global Intelligence Radar · {period}</title>'
                f'<style>{STYLE}</style></head><body><main><header><h1>Global Intelligence Radar</h1>'
                f'<p class="meta">{edition[:10]} · {period} · 北京时间 · 本地验证版</p></header>'
                f'{"".join(parts)}<section><h2>历史简报</h2><ul>{history}</ul></section></main></body></html>')
        (output / f"brief-{edition}.html").write_text(html)
        if latest is None:
            latest = html
    if latest:
        (output / "index.html").write_text(latest)


def card(item):
    level = "BAS"[item["rank"][0]]
    body = "".join(f"<p>{escape(line)}</p>" for line in item["body"])
    sources = "".join(f'<li><a href="{escape(url, quote=True)}" rel="noreferrer">{escape(name)}</a>'
                      f' · {escape(time)}</li>' for name, url, time in item["sources"])
    prior = item.get("prior")
    link = (f'<p><a href="brief-{escape(prior[0])}.html#{escape(prior[1], quote=True)}">此前进展</a></p>'
            if prior else "")
    return (f'<article id="{escape(item["anchor"], quote=True)}"><p class="meta">{level} 级 · '
            f'{escape(item["domain"])}</p><h3>{escape(item["title"])}</h3><p>{escape(item["lead"])}</p>'
            f'<details><summary>展开完整报道</summary>{body}{link}<ul>{sources}</ul></details></article>')
