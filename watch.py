"""人物、企业与官方活动跟踪。"""
from datetime import datetime
from html import escape
import json
import re

from radar import BEIJING, ROOT


def names(text, variations):
    normalized = text.casefold()
    return [name for name, aliases in variations.items()
            if any(re.search(r"(?<![A-Za-z0-9])" + re.escape(alias.casefold()) + r"(?![A-Za-z0-9])", normalized)
                   for alias in aliases)]


def html_link(url, label):
    return f'<a href="{escape(url)}" rel="noopener noreferrer">{escape(label)}</a>'


def watch_html(config, data, slot, translate=None):
    events = json.loads((ROOT / "events.json").read_text())
    ranking_path = ROOT / "rd-ranking.json"
    ranking = json.loads(ranking_path.read_text()) if ranking_path.exists() else {"companies": []}
    candidates = sorted(data, key=lambda row: row["published"], reverse=True)
    recent = [row for row in candidates if datetime.fromisoformat(
        row["published"][:16] if "北京时间" in row["published"] else row["published"][:10]
    ).replace(tzinfo=BEIJING) < slot]
    calendar = []
    for event, details in events.items():
        if details.get("start"):
            label = f'{details["start"]} 至 {details["end"]}（主办方当地日期 · {details["zone"]}）'
        else:
            label = "待确认"
        annotation = details.get("note", "")
        if details.get("end", "9999") < slot.date().isoformat():
            if details.get("official_recap"):
                annotation += " · " + html_link(details["official_recap"], "官方发布回顾")
            # 仅自动汇集匹配会期的公开产出，原始来源不变。
            matches = [row for row in recent
                       if details["start"] <= row["published"][:10] <= details["end"]
                       and re.search(r"launch|release|introduc|发布|推出", row["title"], re.I)
                       and event.casefold() in (row["title"] + " " + row["summary"]).casefold()]
            if matches:
                outputs = []
                for row in matches[:4]:
                    summary = row["summary"]
                    if summary and translate:
                        summary = translate(summary, row.get("language", "en")) or summary
                    outputs.append(f"<li>{html_link(row['url'], row['title'])}"
                                   f"<br>{escape(summary or '来源未提供摘要。')}</li>")
                annotation += ("<details><summary>会期发布与后续观察</summary><ul>"
                               + "".join(outputs) + "</ul><p>可对照原文追踪正式上线范围、"
                               "适用人群和后续采用情况。</p></details>")
            else:
                annotation += " · 本库暂无会期内发布资料"
        calendar.append(f"<li>{html_link(details['url'], event)} · {escape(label)} · "
                        f"{escape(details.get('theme', '主题以官方公告为准'))} {annotation}</li>")
    people = []
    companies = []
    tracked = dict(config.get("strategic_companies", {}))
    tracked.update({company["name"]: company.get("aliases", [company["name"]]) for company in ranking["companies"]})
    for row in recent:
        title = row["title"]
        text = title + " " + row["summary"]
        if row["score"] < 7 or not re.search(r"announce|launch|release|keynote|strategy|speech|发布|演讲|宣布", text, re.I):
            continue
        hit = names(text, config["people"])
        company = names(text, tracked)
        if hit:
            people.append(f"<li>{escape('、'.join(hit))} · {html_link(row['url'], title)}</li>")
        if company:
            companies.append(f"<li>{escape('、'.join(company))} · {html_link(row['url'], title)}</li>")
    board = "".join(f"<li>{item['rank']}. {escape(item['name'])} · "
                    f"{item['eur_million']:,.2f} 百万欧元</li>" if "eur_million" in item else
                    f"<li>{item['rank']}. {escape(item['name'])} · 金额待核</li>"
                    for item in ranking["companies"])
    return (f"<section><h2>重要发布会日历</h2><ul>{''.join(calendar)}</ul></section>"
            f"<section><h2>重点人物与企业</h2><details><summary>人物动态</summary>"
            f"<ul>{''.join(people[:8]) or '<li>本轮暂无符合发布或战略条件的报道。</li>'}</ul></details>"
            f"<details><summary>战略企业与研发投入榜</summary><ul>{''.join(companies[:8])}</ul>"
            f"<p>{escape(ranking.get('note', '排行榜待核对。'))}</p><ol>{board}</ol>"
            f"{html_link(ranking['source'], 'JRC 官方榜单') if ranking.get('source') else ''}</details></section>")
