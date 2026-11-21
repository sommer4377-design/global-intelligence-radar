"""从自动简报选出最新一期，并用同一内容生成首页。"""
from datetime import datetime
from html import escape
import re

from radar import BEIJING


def refresh(directory, now):
    editions = []
    for path in directory.glob("brief-????-??-??-??.html"):
        try:
            slot = datetime.strptime(path.stem, "brief-%Y-%m-%d-%H").replace(tzinfo=BEIJING)
        except ValueError:
            continue
        if slot.hour not in (8, 20):
            continue
        text = path.read_text(encoding="utf-8")
        if "真实来源自动简报" not in text:
            continue
        # 以实际生成内容为准。到期后须重新生成才会替换截点前预览。
        is_final = slot <= now and "截点前预览" not in text.partition("</header>")[0]
        editions.append((slot, path.name, text, is_final))
    editions.sort(reverse=True)
    if not editions:
        return None
    latest = next((item for item in editions if item[3]), editions[0])
    history = []
    for slot, name, _, is_final in editions:
        label = f"{slot:%Y-%m-%d %H:%M}" + ("" if is_final else "（预览）")
        history.append(f'<li><a href="{escape(name)}">{label}</a></li>')
    nav = ('<nav><a href="#latest">本期简报</a> · '
           '<a href="#history">全部历史早晚报</a> · '
           '<a href="#hotspots">新兴热点</a> · '
           '<a href="#catchup">七天补阅</a></nav>')
    archive = ('<section id="history"><h2>历史早晚报</h2><ul>' +
               "".join(history) + '</ul></section>')
    page = latest[2].replace("</header>", nav + "</header>", 1).replace("<main>", "<main id=\"latest\">", 1)
    page = page.replace("<section><h2>你可能还不知道", '<section id="hotspots"><h2>你可能还不知道', 1)
    page = page.replace("<section><h2>最近7天", '<section id="catchup"><h2>最近7天', 1)
    page = page.replace("</main>", archive + "</main>", 1)
    target = directory / "index.html"
    pending = directory / "index.html.tmp"
    pending.write_text(page, encoding="utf-8")
    pending.replace(target)
    return latest[1]
