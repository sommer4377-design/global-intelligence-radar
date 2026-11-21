"""从公开项目与社区信号发现候选；运行时不读取企业名单。"""
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
import json
import re
from urllib.parse import urlencode, urlsplit

from news_web import read

ARTIFACT = {"github.com", "huggingface.co"}
RELEASE = re.compile(r"release|launch|open.weight|model|robot|chip|benchmark|发布|模型|开源|芯片|机器人", re.I)
ACTION = re.compile(
    r"releas(?:e|es|ed)|launch(?:es|ed)?|introduc(?:e|es|ed|ing)|announc(?:e|es|ed)|"
    r"debut(?:s|ed)?|unveil(?:s|ed)?|review|first look|first hands.on|available now|"
    r"(?:starts|ships|shipping|lands) (?:at|in)|发布|推出|亮相|首发|上架|开源|实测|评测|成立|融资|raises|funding", re.I)
SPECULATIVE = re.compile(r"\brumou?rs?\b|\bleaks?\b|could launch|may launch|might launch|"
                         r"best deals|coupon|折扣|优惠|爆料|传闻|或将|预计发布", re.I)
TECH_TOPIC = re.compile(
    r"AI\b|robot|chip|semiconductor|GPU|CPU|drone|EV\b|vehicle|gaming|console|headset|"
    r"handheld|computer|phone|hardware|model|software|open.source|benchmark|batter|startup|"
    r"芯片|半导体|机器人|无人机|汽车|手机|头显|电脑|模型|软件|硬件|电池|创业", re.I)
NAME_WORD = r"(?:[A-Z][A-Za-z0-9+]*(?:[.-][A-Za-z0-9]+)*|[0-9]+[A-Za-z][A-Za-z0-9]*|[0-9]+(?:\.[0-9]+)?)"
NAME_SPAN = re.compile(r"(?<![A-Za-z0-9])" + NAME_WORD + r"(?:[ -]+" + NAME_WORD + r"){0,4}(?![A-Za-z0-9])")
GENERIC = {"the", "a", "an", "new", "introducing", "welcome", "announcing", "first",
           "exclusive", "hands", "on", "look", "review", "ai", "gpu", "cpu", "open",
           "source", "tech", "robot", "model", "gaming", "pc", "drone", "console", "with", "fpv", "handheld",
           "introduces", "unveils", "announces", "launches", "releases", "debuts"}
SHOW = re.compile(r"^Show HN:\s*([A-Za-z][A-Za-z0-9+ .-]{1,39}?)(?:\s+[–—-]\s+|: |, )", re.I)
AMBIGUOUS = GENERIC | {"to", "do", "be", "of", "for", "at", "in", "on", "how", "what",
                       "with", "and", "weekly", "daily", "roundup", "news"}
MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"}
STOP = {"model", "instruct", "base", "main", "tools", "release", "project", "repo", "github", "master"}


def tokens(text):
    return re.findall(r"[a-z]+\d*|\d+|[\u3400-\u9fff]+", text.casefold())


def usable_name(name):
    return bool(set(name.split()) - AMBIGUOUS)


def source_text(item):
    return " ".join((item["title"], item.get("summary") or ""))


def names_from_story(item):
    """用文章内容提取候选名称，不从目标名单生成名称。"""
    headline = item["title"]
    text = source_text(item)
    if item["kind"] == "产品目录":
        label = re.split(r" (?:–|—|-) ", headline, maxsplit=1)[0].strip()
        name = " ".join(tokens(label))
        return {name: label} if 4 <= len(name) <= 45 and usable_name(name) else {}
    if not ACTION.search(text) or SPECULATIVE.search(headline) or not TECH_TOPIC.search(text):
        return {}
    names = {}
    for part in NAME_SPAN.findall(headline):
        words = part.replace("-", " ").split()
        while words and words[0].casefold() in GENERIC:
            words.pop(0)
        while words and words[-1].casefold() in GENERIC:
            words.pop()
        if (not words or all(word.casefold() in AMBIGUOUS for word in words)
                or words[0].casefold() in MONTHS or not words[0][0].isalpha()):
            continue
        if len(words) < 2 and not re.search(r"[a-z][A-Z]|[A-Za-z]\d", words[0]):
            continue
        label = " ".join(words)
        names[" ".join(tokens(label))] = label
    for label in re.findall(r"(?:推出|发布|首发|开源|上架)\s*[「『《“]([\u3400-\u9fff][\u3400-\u9fffA-Za-z0-9 .+-]{1,24})[」』》”]", headline):
        names[" ".join(tokens(label))] = label
    for label in re.findall(
            r"(?:推出|发布|首发|上架)\s*(?:全新|新款)?([\u3400-\u9fff]{2,10}?)"
            r"(?:芯片|处理器|机器人|无人机|手机|应用|模型|汽车)(?=[，。！：；、\s]|$)", headline):
        if label not in {"人工智能", "人形", "消费级", "工业级", "旗舰", "新款", "下一代", "自研"}:
            names[" ".join(tokens(label))] = label
    return names


def identity(url):
    parsed = urlsplit(url or "")
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname not in ARTIFACT or len(parts) < 2:
        return None
    owner, name = parts[:2]
    words = tokens(name.removesuffix(".git"))
    while words and words[-1] in {"instruct", "base", "gguf", "preview"}:
        words.pop()
    if not words or all(word in STOP for word in words):
        return None
    return (" ".join(words), owner, name)


def get_json(url):
    return json.loads(read(url))


def github_candidates(day):
    query = urlencode({"q": f"created:>={day - timedelta(days=30)} stars:>=25 fork:false",
                       "sort": "stars", "order": "desc", "per_page": 20})
    try:
        result = get_json("https://api.github.com/search/repositories?" + query)
    except OSError as error:
        fallback = get_json("https://api.github.com/events?per_page=30")
        return [dict(url="https://github.com/" + item["repo"]["name"], title=item["repo"]["name"],
                     date=item["created_at"], kind="项目", count=None, note=str(error))
                for item in fallback if item.get("type") == "ReleaseEvent"
                and item.get("public") and not item.get("payload", {}).get("release", {}).get("draft")]
    return [dict(url=item["html_url"], title=item["name"], owner=item["owner"]["login"],
                 summary=item.get("description") or "", date=item["created_at"],
                 kind="项目", count=item["stargazers_count"])
            for item in result.get("items", []) if not item.get("fork")]


def hn_archive(start, end):
    query = urlencode({"tags": "story", "numericFilters":
                       f"created_at_i>={int(start.timestamp())},created_at_i<{int(end.timestamp())}",
                       "hitsPerPage": 1000})
    result = get_json("https://hn.algolia.com/api/v1/search_by_date?" + query)
    return [dict(url=story.get("url") or "", title=story.get("title") or "",
                 date=story["created_at"], discussion=f"https://news.ycombinator.com/item?id={story['objectID']}",
                 kind="社区", count=story.get("num_comments") or 0)
            for story in result.get("hits", [])]


def match(name, headline):
    needle = " ".join(tokens(name))
    if re.fullmatch(r"[\u3400-\u9fff]{2,24}", needle):
        return needle in headline
    latin = re.findall(r"[a-z]+\d*|\d+", headline.casefold())
    return len(needle) >= 4 and f" {needle} " in f" {' '.join(latin)} "


def aligned(item, name):
    if SPECULATIVE.search(item["title"]):
        return False
    headline = item["title"]
    parts = name.split()
    short = " ".join(parts[1:]) if len(parts) >= 3 and any(char.isdigit() for char in name) else ""
    return any(ACTION.search(sentence) and
               (match(name, sentence) or (short and match(name, headline) and match(short, sentence)))
               for sentence in [headline, *re.split(r"[。！!?]|\. ", item.get("summary") or "")])


def media_domain(url):
    return (urlsplit(url).hostname or "").removeprefix("www.")


def evaluate(signals, now, older=None, healthy=True, active=None):
    """汇总独立证据；只对真实且可比的观测计算变化。"""
    older = older or {}
    anchors = defaultdict(list)
    media = []
    for item in signals:
        artifact = identity(item["url"])
        if artifact:
            name, owner, artifact_name = artifact
            anchors[name].append((item, owner, artifact_name))
        elif item["url"] and item["kind"] in {"媒体", "社区", "发布方", "产品目录"}:
            media.append(item)
            if item["kind"] in {"媒体", "发布方", "产品目录"}:
                for name, label in names_from_story(item).items():
                    anchors[name].append((item, "", label))
            show = SHOW.search(item["title"]) if item["kind"] == "社区" else None
            if show and media_domain(item["url"]) not in {"", "news.ycombinator.com"}:
                name = " ".join(tokens(show[1]))
                if len(name) >= 4:
                    anchors[name].append((item, media_domain(item["url"]), show[1]))
    findings = []
    for name, entries in anchors.items():
        if not usable_name(name):
            continue
        anchor_urls = {item["url"] for item, _, _ in entries}
        linked = [item for item in media if item["url"] not in anchor_urls and match(name, source_text(item))
                  and not SPECULATIVE.search(item["title"])]
        items = [item for item, _, _ in entries] + linked
        # 同标题转载只记一次；目录和发布方不能充当独立媒体。
        reports = {}
        for item in items:
            if item["kind"] == "媒体":
                reports.setdefault(tuple(tokens(item["title"])), media_domain(item["url"]))
        publishers = set(reports.values())
        launch_reports = {media_domain(item["url"]) for item in items if item["kind"] == "媒体"
                          and aligned(item, name)}
        first_party = {media_domain(item["url"]) for item in items if item["kind"] == "发布方"
                       and aligned(item, name)}
        distinct_action_reports = publishers & launch_reports
        discussions = {item["discussion"]: item["count"] for item in items if item.get("discussion")}
        projects = [item for item, _, _ in entries if item["kind"] == "项目"]
        references = {item["url"] for item, _, _ in entries}
        baseline = older.get(name, {})
        previous_counts = baseline.get("discussion_counts", {})
        shared = discussions.keys() & previous_counts.keys()
        if active is not None:
            shared &= active
        growth = sum(discussions[key] - previous_counts[key] for key in shared) if shared and healthy else None
        release = bool(projects) or any(urlsplit(url).hostname == "huggingface.co" for url in references)
        # 项目仓库证明代码已可访问；不代表存在正式产品版。
        relevant = any(TECH_TOPIC.search(source_text(item)) or RELEASE.search(source_text(item)) for item in items)
        attention = sum(discussions.values())
        verified = bool(publishers) and (
            bool(first_party - publishers) or
            (len(distinct_action_reports) >= 2 and relevant) or
            (release and (len(publishers) >= 2 or attention >= 15) and relevant)
        )
        warming = (len(publishers) >= 2 or (publishers and (release or attention >= 15))
                   or (growth is not None and growth >= 10))
        state = "已验证热点" if verified else "持续升温" if warming else "首次发现"
        score = (int(verified) * 7 + int(bool(first_party or release)) * 2 +
                 min(len(publishers), 3) + int(attention >= 15) + int(growth is not None and growth >= 10))
        owners = {}
        for _, owner, _ in entries:
            if owner:
                owners.setdefault(owner.casefold(), owner)
        aliases = set()
        for item in linked:
            context = item["title"] + " " + item.get("summary", "")
            for label, latin in re.findall(
                    r"([\u3400-\u9fff]{2,10})[（(]([A-Za-z][A-Za-z0-9 .-]+)[）)]", context):
                if "".join(tokens(latin)) in owners:
                    aliases.update((label, latin))
            prefix = re.match(r"([\u3400-\u9fff]{2,10})(?:发布|推出)", item["title"])
            if prefix and any(match(name, other["title"]) for other in linked if other is not item):
                aliases.add(prefix[1])
        dated = [datetime.fromisoformat(item["date"].replace("Z", "+00:00")) for item in items]
        first, latest = min(dated).isoformat(), max(dated).isoformat()
        urls = sorted({item["url"] for item in items} |
                      {item["discussion"] for item in items if item.get("discussion")})
        findings.append(dict(name=name, display=entries[0][2], owners=sorted(owners.values()), aliases=sorted(aliases),
                             state=state, score=score, growth=growth, first=first, latest=latest,
                             discussion_counts=discussions, publishers=sorted(publishers),
                             public_artifact=release, official=sorted(first_party), verified=verified,
                             relevance=relevant, urls=urls, seen=now.isoformat()))
    # 同一证据组的短名，例如产品后缀，优先显示更具体的名称。
    ranked = sorted(findings, key=lambda item: (-item["score"], -len(item["name"]), item["name"]))
    unique = []
    for item in ranked:
        if any(item["urls"] == other["urls"] and
               (match(item["name"], other["name"]) or match(other["name"], item["name"]))
               for other in unique):
            continue
        unique.append(item)
    return unique


def report_html(findings, slot):
    def age(row):
        value = datetime.fromisoformat(row["latest"].replace("Z", "+00:00"))
        return slot - value

    findings = [row for row in findings if datetime.fromisoformat(row["seen"]) <= slot
                and not row.get("withdrawn") and usable_name(row["name"])]
    qualified = [row for row in findings if row.get("verified", row["score"] >= 7) and timedelta(0) <= age(row) <= timedelta(days=30)]
    featured = [row for row in qualified if age(row) <= timedelta(days=7)][:3]
    early = [row for row in findings if not row.get("verified", row["score"] >= 7)
             and timedelta(0) <= age(row) <= timedelta(days=7)
             and (row["publishers"] or row.get("official"))][:5]
    retrospective = [row for row in qualified if timedelta(days=7) < age(row) <= timedelta(days=30)][:12]

    def card(row):
        links = " · ".join(f'<a href="{escape(url)}">{escape(media_domain(url))}</a>'
                           for url in row["urls"] if urlsplit(url).scheme in {"https", "http"})
        growth = "历史基线不足" if row["growth"] is None else f"同帖讨论数净变化 {row['growth']:+d}"
        return (f"<article><h3>{escape(row['display'])} · {row['state']}</h3>"
                f"<p>{'关联项目所有者：' + escape('、'.join(row['owners'])) + '。' if row['owners'] else ''}"
                f"独立媒体域 {len(row['publishers'])} 个；讨论样本 {len(row['discussion_counts'])} 个；"
                f"{growth}。{'有公开作品证据。' if row['public_artifact'] else ''}"
                f"{'有发布方公告。' if row.get('official') else ''}"
                f"{'媒体候选别名：' + escape('、'.join(row['aliases'])) + '。' if row['aliases'] else ''}"
                f"</p><p>证据日期 {escape(row.get('first', row['latest'])[:10])} 至 "
                f"{escape(row['latest'][:10])}（各来源当地公示日期或归档时间）</p>"
                f"<p>{links}</p></article>")

    return (f'<section><h2>你可能还不知道</h2><div class="cards">'
            f'{"".join(map(card, featured)) or "本期暂无达到证据门槛的新兴热点。"}</div>'
            f'<details><summary>早期线索 · 待继续核实</summary><div class="cards">'
            f'{"".join(map(card, early)) or "暂无符合条件的早期线索。"}</div></details>'
            f'<details><summary>热点观察池与 7/30 天回溯</summary><div class="cards">' 
            f'{"".join(map(card, retrospective + [row for row in findings if not row.get("verified", row["score"] >= 7) and row not in early][:10])) or "暂无其他观测。"}'
            '</div></details></section>')
