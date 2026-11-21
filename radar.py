"""本地简报预览。仅使用 Python 标准库。"""
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
from urllib.request import urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
HN = "https://hacker-news.firebaseio.com/v0/"


def read_url(url):
    with urlopen(url, timeout=4) as response:
        return response.read(1_000_000)


def collect(config):
    rows = []
    errors = []
    for name, url in config["feeds"].items():
        try:
            root = ET.fromstring(read_url(url))
            for item in root.findall("./channel/item")[:30]:
                try:
                    published = parsedate_to_datetime(item.findtext("pubDate", ""))
                    title, link = item.findtext("title"), item.findtext("link")
                    if title and link and published.tzinfo:
                        rows.append(dict(title=title, url=link, source=name,
                                         published=published.isoformat(), category="全球重大事件",
                                         candidate=True))
                except ValueError:
                    errors.append(f"{name}：有条目缺少可解析的发布时间")
        except (OSError, ValueError, ET.ParseError) as error:
            errors.append(f"{name}：{error}")
    try:
        for story_id in json.loads(read_url(HN + "showstories.json"))[:5]:
            try:
                story = json.loads(read_url(f"{HN}item/{story_id}.json"))
                rows.append(dict(title=story["title"], source="Hacker News",
                                 published=datetime.fromtimestamp(story["time"], timezone.utc).isoformat(),
                                 url=f"https://news.ycombinator.com/item?id={story_id}",
                                 category="全球科技商业", candidate=True))
            except (OSError, ValueError, TypeError, KeyError) as error:
                errors.append(f"Hacker News 条目 {story_id}：{error}")
    except (OSError, ValueError) as error:
        errors.append(f"Hacker News：{error}")
    return rows, errors


def render(config, rows, slot, sample):
    start = slot - timedelta(hours=12)
    report = [f"# {slot:%Y-%m-%d %H:%M} 北京时间资讯预览"]
    if sample:
        report.append("以下为合成样本，活动日期和消息均为测试输入。")
    sections = {name: [] for name in (
        "今日最重要的科技新闻", "值得关注的新 AI 产品与技术", "重要人物动态",
        "即将举行的重要发布会", "全球重大事件及华为全产业资讯", "待核实线索")}
    seen = set()
    for row in sorted(rows, key=lambda record: record.get("priority", "P2")):
        published = datetime.fromisoformat(row["published"])
        if not start <= published < slot or row["url"] in seen:
            continue
        seen.add(row["url"])
        title = row["title"]
        people = [name for name, aliases in config["people"].items()
                  if any(alias.casefold() in title.casefold() for alias in aliases)]
        if row.get("candidate"):
            section = "待核实线索"
        elif row["category"] in ("全球重大事件", "华为全产业"):
            section = "全球重大事件及华为全产业资讯"
        elif people:
            section = "重要人物动态"
        elif row["category"] == "全球 AI":
            section = "值得关注的新 AI 产品与技术"
        else:
            section = "今日最重要的科技新闻"
        tags = " · ".join([row["category"], *people])
        sections[section].append(
            f"- {title}（{tags} · {published.astimezone(BEIJING):%m-%d %H:%M}）"
            f" [{row['source']}]({row['url']})")
    # 已核实记录由人工查阅主办方公告后录入。
    for event in config["events"]:
        verified = config.get("verified_events", {}).get(event)
        deadline = datetime.fromisoformat(verified["start"]) if verified else None
        if verified and verified.get("official_url", "").startswith("https://") and deadline.tzinfo:
            if deadline < slot:
                continue
            event_text = (
                f"- {event} · {deadline.astimezone(BEIJING):%Y-%m-%d %H:%M} 北京时间"
                f" · [主办方公告]({verified['official_url']})"
            )
        else:
            event_text = f"- {event} · 本轮无已核实的举办时间"
        sections["即将举行的重要发布会"].append(event_text)
    for section, entries in sections.items():
        report.extend(["", f"## {section}", "", *(entries or ["本期暂无条目。"])])
    return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", required=True, help="北京时间，例如 2026-11-21T08:00")
    parser.add_argument("--live", action="store_true", help="读取公开来源")
    args = parser.parse_args()
    slot = datetime.fromisoformat(args.slot)
    if slot.tzinfo is not None or (slot.hour, slot.minute, slot.second, slot.microsecond) not in (
        (8, 0, 0, 0), (20, 0, 0, 0)
    ):
        parser.error("请填写北京时间 08:00 或 20:00，省略时区")
    config = json.loads((ROOT / "config.json").read_text())
    rows, errors = collect(config) if args.live else (
        json.loads((ROOT / "sample.json").read_text()), []
    )
    print(render(config, rows, slot.replace(tzinfo=BEIJING), not args.live))
    if errors:
        print("\n## 来源读取问题\n")
        for error in errors:
            print(f"- {error}")


if __name__ == "__main__":
    main()
