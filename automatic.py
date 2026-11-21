"""使用真实公开来源生成本地早晚报。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from decimal import Decimal
from html import escape
import json
from pathlib import Path
import re
import resource
import sqlite3
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from news_web import fetch
from sources import REGISTRY
import discovery
from watch import watch_html
from homepage import refresh
from publish import CATEGORIES, card
from radar import BEIJING, ROOT

LOW = r"mega-sena|loteria|atendimento presencial|bolsa família|feriadão|horário de funcionamento"
GLOBAL = r"ceasefire|refugee|genocide|climate (?:deal|finance)|food (?:crisis|programme)|sanctions|earthquake|election|ukraine|west bank|terremoto|alto el fuego|refugiados|elecciones|sanciones|guerra|inundaciones|cumbre|sanções|terremoto|cessar-fogo|refugiados|genocídio|crise alimentar"
TECH = r"launch|introduc|release|deploy|benchmark|preview|available|acquisit|funding|announc|发布|上线"
RIVALS = r"google|gemini|apple|samsung|nvidia|qualcomm|ericsson|nokia|xiaomi|honor|tesla|zte|microsoft"
COMMERCIAL = r"financial|earnings|revenue|acquisit|investment|commercial|supply chain|funding|融资|财报"
WHY = {
    "全球重大事件": "这条报道涉及公共安全、人道援助或国际政策。后续可关注责任机构通报及影响范围。",
    "华为全产业": "这是观察华为产品、产业合作或技术商用进展的线索。具体覆盖和效果以来源披露为准。",
    "华为关联友商": "该动向涉及相关企业的产品或技术能力，可用于跟踪行业竞争。",
    "全球 AI": "该动向可帮助跟踪人工智能工具、新功能及应用场景。",
    "全球科技商业": "该报道提供了观察科技商业化、企业经营或市场变化的线索。",
}
EVENTS = r"Meta Connect|Google I/O|Apple WWDC|OpenAI DevDay|NVIDIA GTC|\bCES\b|\bMWC\b"


def canonical(url):
    parts = urlsplit(url)
    query = urlencode(sorted((key, val) for key, val in parse_qsl(parts.query)
                            if not key.startswith("utm_") and key not in {"fbclid", "gclid"}))
    path = parts.path.replace("/feed/view/en/story/", "/en/story/").rstrip("/")
    return urlunsplit(("https", parts.netloc.lower(), path, query, ""))


class MosesBpe:
    """西语模型使用 Moses 与 BPE，其他现有模型使用 SentencePiece。"""

    def __init__(self, path):
        from sacremoses import MosesDetokenizer, MosesPunctNormalizer, MosesTokenizer
        from subword_nmt.apply_bpe import BPE
        self.normalizer = MosesPunctNormalizer(lang="es")
        self.tokenizer = MosesTokenizer(lang="es")
        self.detokenizer = MosesDetokenizer(lang="en")
        with path.open(encoding="utf-8") as codes:
            self.bpe = BPE(codes)

    def encode(self, text, out_type=str):
        return self.bpe.segment_tokens(self.tokenizer.tokenize(self.normalizer.normalize(text)))

    def decode(self, tokens):
        return self.detokenizer.detokenize(" ".join(tokens).replace("@@ ", "").split())


class Translator:
    """用已下载的 Argos 模型执行本地推理。"""

    def __init__(self, directory):
        self.stages = {}
        if not directory:
            return
        try:
            import ctranslate2
            import sentencepiece
            for pair in ("en_zh", "pt_en", "es_en"):
                for candidate in directory.glob("*/metadata.json"):
                    meta = json.loads(candidate.read_text())
                    if f"{meta['from_code']}_{meta['to_code']}" == pair:
                        model = candidate.parent
                        try:
                            pieces = model / "sentencepiece.model"
                            encoder = (sentencepiece.SentencePieceProcessor(model_file=str(pieces))
                                       if pieces.exists() else MosesBpe(model / "bpe.model"))
                            self.stages[pair] = (
                                encoder,
                                ctranslate2.Translator(str(model / "model"), device="cpu", intra_threads=2),
                            )
                        except (ImportError, OSError, RuntimeError, ValueError) as error:
                            print(f"{pair} 模型加载失败：{error}，对应语言使用原文。")
                        break
        except (ImportError, OSError, RuntimeError, ValueError, KeyError) as error:
            print(f"离线模型加载失败，将对不可用语言使用原文：{error}")

    def translate(self, text, language):
        pairs = {"en": ["en_zh"], "pt": ["pt_en", "en_zh"], "es": ["es_en", "en_zh"], "zh": []}
        route = pairs.get(language)
        if not text or route is None or any(pair not in self.stages for pair in route):
            return None
        try:
            for pair in route:
                encoder, engine = self.stages[pair]
                tokens = encoder.encode(text, out_type=str)
                result = engine.translate_batch([tokens], beam_size=2, max_decoding_length=360)[0]
                text = encoder.decode(result.hypotheses[0]).strip()
        except (RuntimeError, ValueError, IndexError):
            return None
        text = text.replace("▁", " ").strip()
        for original, normalized in {"双子座": "Gemini", "罗辛亚": "罗兴亚",
                                     "冷冻教室": "寒冷的教室"}.items():
            text = text.replace(original, normalized)
        return text if re.search(r"[\u3400-\u9fff]", text) else None


def classify(row, config):
    text = " ".join((row["title"], row["summary"]))
    has = lambda pattern: bool(re.search(pattern, text, re.I))
    category = row["category"]
    tags = [person for person, aliases in config["people"].items()
            if any(alias.casefold() in text.casefold() for alias in aliases)]
    if has(EVENTS):
        tags.append("科技活动")
    if category == "全球重大事件":
        if has(LOW) or not has(GLOBAL):
            return CATEGORIES[0], 0, tags
        if row["source"].startswith("GDACS") and not has(r"6\.[5-9]M|\b[789]\.\dM|\bred\b|\borange\b"):
            return CATEGORIES[0], 0, tags
        score = 6 + 3 * has(r"genocide|ceasefire|food crisis|climate deal|sanctions")
    elif re.search(r"本周看什么|最近值得一看的.*部作品|what to watch|streaming schedule", row["title"], re.I):
        return CATEGORIES[4], 0, tags
    elif category == "华为" or has(r"\bhuawei\b|华为|昇腾|鸿蒙|乾崑"):
        category = CATEGORIES[1]
        score = 5 + 3 * (has(TECH) and not has(r"documentary|纪录片"))
    else:
        if has(r"black friday deals|coupon code|lowest price ever|折扣码|好价汇总|优惠券"):
            return CATEGORIES[4], 0, tags
        category = CATEGORIES[2] if has(RIVALS) else CATEGORIES[4] if has(COMMERCIAL) or (row["category"] == CATEGORIES[4] and not has(r"\bAI\b|模型|人工智能|大模型|LLM")) else CATEGORIES[3]
        score = 4 + 3 * has(TECH) + has(COMMERCIAL)
    return category, score, tags


def timestamp(label):
    if "北京时间" in label:
        return datetime.strptime(label[:16], "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
    return datetime.strptime(label[:10], "%Y-%m-%d").replace(tzinfo=BEIJING)


def event_time(row):
    return datetime.fromisoformat(row["updated"]) if row.get("updated") else timestamp(row["published"])


def store(db, row, now):
    key = row["key"]
    payload = db.execute("SELECT payload FROM articles WHERE url = ?", (key,)).fetchone()
    if payload:
        old = json.loads(payload[0])
        if old.get("updated"):
            row["updated"] = old["updated"]
        before, after = old["summary"], row["summary"]
        if (old["score"] >= 7 and before and after and
                (SequenceMatcher(None, before, after).ratio() < 0.8 or
                 re.findall(r"\d+(?:[.,]\d+)?", before) != re.findall(r"\d+(?:[.,]\d+)?", after))):
            db.execute("INSERT INTO revisions VALUES (?, ?, ?)", (key, now.isoformat(), payload[0]))
            row["updated"] = now.isoformat()
    db.execute("INSERT INTO articles VALUES (?, ?) ON CONFLICT(url) "
               "DO UPDATE SET payload=excluded.payload", (key, json.dumps(row, ensure_ascii=False)))


MODEL_ID = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z-]{0,24}[- ]?\d+(?:[.]\d+)?[A-Za-z]*"
    r"(?:[ -](?:pro|max|ultra|plus|mini|lite|se|air))*)(?![A-Za-z0-9])", re.I)
RELEASE_ACTION = re.compile(r"announce|unveil|introduc|release|launch|debut|发布|亮相|推出|lança|lanza|lance", re.I)
SHIP_ACTION = re.compile(r"available now|goes on sale|starts shipping|现已发售|开售|现已上市|正式发售", re.I)
RECALL_ACTION = re.compile(r"recall|召回|撤回|retirada", re.I)
COMMON_ID = {"january", "february", "march", "april", "may", "june", "july", "august",
             "september", "october", "november", "december", "version", "phase", "figure",
             "up", "to", "for", "in", "at", "by", "on", "of", "from", "than", "about"}


def model_ids(title):
    return frozenset(re.sub(r"[ -]", "", model.casefold()) for model in MODEL_ID.findall(title)
                     if model.split()[0].casefold() not in COMMON_ID
                     and not re.search(r"\b20[0-9]{2}\b", model))


def event_action(title):
    if RECALL_ACTION.search(title):
        return "recall"
    if SHIP_ACTION.search(title):
        return "shipping"
    if RELEASE_ACTION.search(title):
        return "release"
    return ""


def group_events(rows):
    groups = []
    for row in sorted(rows, key=lambda item: (event_time(item), item["key"]), reverse=True):
        title = row["title"]
        words = set(re.findall(r"[a-z0-9]{3,}|[\u3400-\u9fff]+", title.casefold()))
        ids, action = model_ids(title), event_action(title)
        for group in groups:
            head, head_words, head_ids, head_action = group[0]
            if (head["category"] != row["category"] or
                    abs(timestamp(row["published"]) - timestamp(head["published"])) > timedelta(days=3) or
                    (action and head_action and action != head_action) or
                    (ids and head_ids and not (ids & head_ids))):
                continue
            cross_language = (row.get("language") != head.get("language") and action and action == head_action
                              and bool(ids & head_ids))
            similar = min(len(words), len(head_words)) >= 4 and (
                len(words & head_words) / len(words | head_words) >= 0.70 or
                SequenceMatcher(None, title.casefold(), head["title"].casefold()).ratio() >= 0.88)
            if cross_language or similar or title.casefold() == head["title"].casefold():
                group.append((row, words, ids, action))
                break
        else:
            groups.append([(row, words, ids, action)])
    return [[item[0] for item in group] for group in groups]


NUMBER_SCALE = {"thousand": 1000, "million": 1000000, "billion": 1000000000,
                "万": 10000, "亿": 100000000}
CALENDAR_MONTH = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})\b",
    re.I)
MONTH_NUMBER = {name: number for number, name in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def protected_numbers(text):
    text = CALENDAR_MONTH.sub(
        lambda match: f"{MONTH_NUMBER[match[1][:3].casefold()]}月{match[2]}日", text)
    text = re.sub(r"(?<=\d)[,， ](?=\d{3}(?:\D|$))", "", text)
    return {Decimal(value) * NUMBER_SCALE.get(scale.casefold(), 1)
            for value, scale in re.findall(r"(\d+(?:[.]\d+)?)(?:\s*(thousand\b|million\b|billion\b|万|亿))?",
                                           text, re.I)}


def calendar_dates(text):
    dates = {(MONTH_NUMBER[item[1][:3].casefold()], int(item[2]))
             for item in CALENDAR_MONTH.finditer(text)}
    dates.update((int(month), int(day)) for month, day in re.findall(r"(\d{1,2})月\s*(\d{1,2})[日号]", text))
    dates.update((int(month), int(day)) for month, day in re.findall(r"\b20\d{2}-(\d{2})-(\d{2})\b", text))
    return dates


def technical_units(text):
    equivalents = {"千兆赫": "ghz", "兆赫": "mhz", "赫兹": "hz", "纳米": "nm",
                   "毫秒": "ms", "瓦": "w", "伏": "v", "％": "%"}
    pattern = r"(\d+(?:[.]\d+)?)\s*(GHz|MHz|Hz|GB|MB|TB|nm|ms|W|V|%|％|千兆赫|兆赫|赫兹|纳米|毫秒|瓦|伏)(?![A-Za-z])"
    return {(Decimal(amount), equivalents.get(unit.casefold(), unit.casefold()))
            for amount, unit in re.findall(pattern, text, re.I)}


def translation_issue(original, translated):
    if translated is None:
        return ""
    if protected_numbers(original) != protected_numbers(translated):
        return "数字一致性"
    dates = calendar_dates(original)
    if dates and dates != calendar_dates(translated):
        return "日期顺序"
    units = technical_units(original)
    if units and not units <= technical_units(translated):
        return "规格单位"
    source_models = model_ids(original)
    normalized = re.sub(r"[^a-z0-9.]", "", translated.casefold())
    # 保护带型号的直写字段；数字前的普通英文词由字形过滤。
    guarded = {item for item in source_models if re.match(r"^[a-z]+[0-9]", item)}
    named = re.findall(r"(?<![A-Za-z0-9])(?:[A-Z]{2,}[ -]?\d+|[A-Z][a-zA-Z]+[ -]\d+)",
                       original)
    explicit = {item for item in guarded if any(item.startswith(re.sub(r"[ -]", "", token.casefold()))
                                               for token in named)}
    if any(item not in normalized for item in explicit):
        return "产品型号"
    if re.search(r"\b(?:did not|will not|is not available|cannot)\b", original, re.I) and not re.search(
            r"不|未|无|无法|不能|没有", translated):
        return "关键否定"
    return ""


def render_item(group, translator, number):
    row = group[0]
    title = translator.translate(row["title"], row["language"])
    summary = translator.translate(row["summary"], row["language"])
    overview = summary or ("暂无可用的中文摘要，请展开查看来源原文。" if row["summary"] else
                           "来源未提供摘要，请阅读原文。")
    title_issue = translation_issue(row["title"], title)
    summary_issue = translation_issue(row["summary"], summary)
    if title_issue:
        title = None
    if summary_issue:
        summary = None
        overview = f"译文未通过{summary_issue}校验，请展开来源原文。"
    why = WHY[row["category"]]
    original = f"{row['title']} {row['summary']}"
    if row["category"] == CATEGORIES[0]:
        if re.search(r"refugee|genocide|refugiados|genocídio", original, re.I):
            why = "报道涉及难民处境或国际司法，值得跟踪相关机构如何落实救援或法律决定。"
        elif re.search(r"earthquake|terremoto", original, re.I):
            why = "地震警报提供震级和潜在影响线索，值得对照当地机构后续通报。"
        elif re.search(r"climate|COP31", original, re.I):
            why = "谈判动向可用于跟踪各方气候承诺与融资安排。"
    elif re.search(r"preview|new features|launch|introduct|introduc", original, re.I):
        why += " 来源提到发布或预览，值得核对可用地区、开放阶段及适用对象。"
    if row.get("updated"):
        why += " 来源摘要出现实质变化，已作为更新纳入。"
    if row["tags"]:
        why += " 关联监测项为" + "、".join(row["tags"]) + "。"
    why += f" 规则分 {row['score']}；由来源栏目及发布、商业或公共影响信号产生。"
    priority = "P0" if row["score"] >= 9 else "P1" if row["score"] >= 7 else "P2"
    info = dict(
        title=title or "原文标题 · " + row["title"], overview=overview,
        why=why, reading_url=row["url"], original_title=row["title"],
        original_summary=row["summary"], priority=priority,
        meta=f"{row['source']} · {row['published']} · " + ("来源中文" if row["language"] == "zh" else "离线机翻" if title else
              f"原文回退（{title_issue}）" if title_issue else "原文回退"),
        language={"en": "英语", "pt": "葡萄牙语", "es": "西班牙语", "zh": "中文"}[row["language"]],
    )
    html = card(info, number)
    if len(group) > 1:
        related = "".join(
            f'<li><a href="{escape(item["url"])}">{escape(item["source"])}</a>'
            f' · {escape(item["published"])} · {escape(item["title"])}</li>' for item in group[1:]
        )
        html = html.replace("</article>", f"<details><summary>相关来源</summary><ul>{related}</ul></details></article>")
    return info["title"], html, bool(title), bool(summary)


def write_report(slot, data, translator, target, statuses, findings=(), watch=""):
    start = slot - timedelta(hours=12)
    seven_days = slot - timedelta(days=7)
    recent = [row for row in data if row["score"] and seven_days <= event_time(row) < slot]
    groups = group_events(recent)
    rank = lambda group: (group[0]["score"], event_time(group[0]))
    primary = [group for group in groups if event_time(group[0]) >= start
               and (group[0].get("updated") or all(event_time(row) >= start for row in group[1:]))]
    primary = sorted(primary, key=rank, reverse=True)[:15]
    missed = [group for group in groups if group not in primary and group[0]["score"] >= 7]
    missed = sorted(missed, key=rank, reverse=True)[:12]
    sections = {name: [] for name in CATEGORIES}
    highlights, catchup = [], []
    titles = summaries = 0
    for number, group in enumerate(primary + missed, 1):
        translated, html, has_title, has_summary = render_item(group, translator, number)
        titles += has_title
        summaries += has_summary
        if number <= len(primary):
            sections[group[0]["category"]].append(html)
            if number <= 5 and group[0]["score"] >= 7:
                highlights.append(f'<li><a href="#news-{number}">{escape(translated)}</a></li>')
        else:
            catchup.append(html)
    blocks = "".join(f'<section><h2>{name}</h2><div class="cards">{"".join(items) or "本期暂无入选。"}</div></section>'
                     for name, items in sections.items())
    css = (ROOT / "news.css").read_text()
    health = "".join(f"<li>{escape(line)}</li>" for line in statuses)
    target.write_text(f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Intelligence Radar 自动简报</title><style>{css}</style>
<header><h1>Global Intelligence Radar</h1><p>{slot:%Y-%m-%d %H:%M} 北京时间 · 真实来源自动简报{" · 截点前预览" if slot > datetime.now(BEIJING) else ""}</p></header>
<main><p>重要性来自规则评分，可关注的理由为分类提示。机器翻译请对照原文。日期精度仅到日的报道按标示日期归入相应窗口。</p>
<section class="lead"><h2>今日重点</h2><ol>{''.join(highlights) or '<li>本期暂无高分条目。</li>'}</ol></section>
{discovery.report_html(findings, slot)}{watch}{blocks}<section><h2>最近7天可能错过的重要新闻</h2><div class="cards">
{''.join(catchup) or '暂无符合条件的补阅内容。'}</div></section>
<details><summary>本轮采集状态</summary><ul>{health}</ul></details></main></html>""", encoding="utf-8")
    print(f"{target.name}：本期 {len(primary)} 条，补阅 {len(missed)} 条；"
          f"中文标题 {titles}/{len(primary + missed)}，中文摘要 {summaries}/{len(primary + missed)}，"
          f"七天窗口 {len(recent)} 篇归为 {len(groups)} 组")


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="北京时间日期，如 2026-11-21")
    parser.add_argument("--slot", choices=("08", "20", "both"), default="both")
    parser.add_argument("--models", type=Path)
    parser.add_argument("--state", type=Path, default=ROOT / "local-news.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--no-community", action="store_true", help="跳过 Hacker News 查询")
    parser.add_argument("--reuse", action="store_true", help="用已采集数据重新生成")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=BEIJING)
    config = json.loads((ROOT / "config.json").read_text())
    statuses = ["本轮使用本地留存的数据重新生成。"] if args.reuse else []
    with sqlite3.connect(args.state) as db:
        db.execute("CREATE TABLE IF NOT EXISTS articles(url TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS revisions(url TEXT, noticed_at TEXT, previous TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS health(source TEXT, checked TEXT, ok INTEGER, latest TEXT, error TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS signals(key TEXT PRIMARY KEY, observed TEXT, payload TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS findings(key TEXT PRIMARY KEY, payload TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS finding_history(key TEXT, seen TEXT, payload TEXT, "
                   "PRIMARY KEY(key, seen))")
        now = datetime.now(BEIJING)
        if not args.reuse:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda source: fetch(source[:3], limit=12), REGISTRY))
            community, github = [], []
            community_ok = False
            if not args.no_community:
                for name, operation in (
                    ("Hacker News", lambda: discovery.hn_archive(now - timedelta(hours=36), now)),
                    ("GitHub", lambda: discovery.github_candidates(now.date())),
                ):
                    try:
                        incoming = operation()
                        latest = max((item["date"] for item in incoming), default="")
                        db.execute("INSERT INTO health VALUES (?, ?, ?, ?, ?)",
                                   (name, now.isoformat(), 1, latest, ""))
                        cap = "（达到返回上限，样本不完整）" if name == "Hacker News" and len(incoming) == 1000 else ""
                        statuses.append(f"{name}：{len(incoming)} 条信号{cap}")
                        if name == "Hacker News":
                            community, community_ok = incoming, True
                        else:
                            github = incoming
                    except (OSError, ValueError, KeyError) as error:
                        db.execute("INSERT INTO health VALUES (?, ?, ?, ?, ?)",
                                   (name, now.isoformat(), 0, "", str(error)))
                        statuses.append(f"{name}：{error}")
            signals = community + github
            for record, (_, rows, error) in zip(REGISTRY, results):
                source, _, _, _, language, role, _ = record
                latest = max((row["published"] for row in rows), default="")
                db.execute("INSERT INTO health VALUES (?, ?, ?, ?, ?)",
                           (source, now.isoformat(), int(bool(rows)), latest, error))
                for row in rows:
                    key = canonical(row["url"])
                    category, score, tags = classify(row, config)
                    row.update(key=key, category=category, score=score, tags=tags, language=language, role=role)
                    if role in {"媒体", "官方", "社区", "产品目录"} and category != CATEGORIES[0]:
                        signals.append(dict(url=row["url"], title=row["title"], summary=row["summary"],
                                            date=timestamp(row["published"]).isoformat(),
                                            kind="发布方" if role == "官方" else role))
                    if role in {"社区", "产品目录"}:
                        row["score"] = 0
                    store(db, row, now)
            for item in signals:
                if not item["url"]:
                    continue
                key = item.get("discussion") or item["url"]
                db.execute("INSERT OR REPLACE INTO signals VALUES (?, ?, ?)",
                           (key, now.isoformat(), json.dumps(item, ensure_ascii=False)))
            floor = (now - timedelta(days=30)).isoformat()
            db.execute("DELETE FROM signals WHERE observed < ?", (floor,))
            recent_signals = [item for value, in db.execute("SELECT payload FROM signals")
                              if (item := json.loads(value)) and
                              now - timedelta(days=30) <= datetime.fromisoformat(item["date"].replace("Z", "+00:00")) <= now]
            prior = {key: json.loads(value) for key, value in db.execute("SELECT key, payload FROM findings")}
            active = {item["discussion"] for item in community if item.get("discussion")}
            findings = discovery.evaluate(recent_signals, now, prior, community_ok, active)
            current_keys = {item["name"] for item in findings}
            for name in prior.keys() - current_keys:
                # 一旦证据过期或规则撤回候选，旧推荐不能在后续报纸里复现。
                retired = dict(prior[name], withdrawn=True, verified=False, seen=now.isoformat())
                db.execute("INSERT OR REPLACE INTO finding_history VALUES (?, ?, ?)",
                           (name, retired["seen"], json.dumps(retired, ensure_ascii=False)))
                db.execute("DELETE FROM findings WHERE key = ?", (name,))
            for item in findings:
                db.execute("INSERT OR REPLACE INTO findings VALUES (?, ?)",
                           (item["name"], json.dumps(item, ensure_ascii=False)))
            db.execute("DELETE FROM health WHERE checked < ?", (floor,))
        else:
            findings = [json.loads(value) for value, in db.execute("SELECT payload FROM findings")]
        for item in findings:
            db.execute("INSERT OR IGNORE INTO finding_history VALUES (?, ?, ?)",
                       (item["name"], item["seen"], json.dumps(item, ensure_ascii=False)))
        db.execute("DELETE FROM finding_history WHERE seen < ?", ((now - timedelta(days=30)).isoformat(),))
        snapshots = [json.loads(value) for value, in db.execute(
            "SELECT payload FROM finding_history ORDER BY seen")]
        data = [json.loads(value) for value, in db.execute("SELECT payload FROM articles")]
        for name, _, _, region, language, role, fields in REGISTRY:
            sample = db.execute("SELECT SUM(ok), COUNT(*) FROM health WHERE source = ?", (name,)).fetchone()
            recent = db.execute("SELECT latest,error FROM health WHERE source = ? ORDER BY checked DESC LIMIT 1",
                                (name,)).fetchone()
            if recent:
                latest, error = recent
                statuses.append(f"{name} · {region}/{language}/{role} · {fields} · "
                                f"成功 {sample[0]}/{sample[1]} 次 · 最近批次 {latest or error}")
    print(f"热点观察池 {len(findings)} 项，证据达标 {sum(item.get('verified', item['score'] >= 7) for item in findings)} 项")
    if not data:
        raise SystemExit("未取得真实数据，保留现有网页。")
    if not statuses:
        statuses.append("使用本地数据库重生成，本轮未重新采集。")
    print(*statuses, sep="\n")
    print(f"本地累计 {len(data)} 个唯一新闻地址")
    translator = Translator(args.models)
    for hour in (("08", "20") if args.slot == "both" else (args.slot,)):
        slot = day.replace(hour=int(hour))
        as_of = {item["name"]: item for item in snapshots if datetime.fromisoformat(item["seen"]) <= slot}
        ranked_findings = sorted(as_of.values(), key=lambda item: (-item["score"], item["name"]))
        write_report(slot, data, translator, args.output_dir / f"brief-{args.date}-{hour}.html",
                     statuses, ranked_findings, watch_html(config, data, slot, translator.translate))
    current = refresh(args.output_dir, datetime.now(BEIJING))
    if current:
        print(f"自动首页采用 {current}")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    mib = peak / (1024 ** 2 if sys.platform == "darwin" else 1024)
    print(f"运行时间 {time.monotonic() - started:.2f} 秒；Python 进程峰值 RSS {mib:.1f} MiB。")


if __name__ == "__main__":
    main()
