"""Bounded evidence retrieval and conservative, language-neutral claim matching."""
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit, urljoin
from subprocess import TimeoutExpired, run


ACTIONS = {
    "agreement": r"\b(?:agree(?:s|d)?|sign(?:s|ed)?|accord|pact|conveni|acuerd\w+|acordo)\b|达成|签署|签订",
    "regulation": r"\b(?:ban(?:s|ned)?|sanction\w*|approv\w+|regulat\w+|proh[ií]b\w+|sancion\w+|aprova\w+)\b|制裁|批准|通过.{0,6}法|禁令",
    "acquisition": r"\b(?:buy|buys|bought|acquir\w+|acquisit\w+|merger|compra\w*|aquisi\w+)\b|收购|合并|并购",
    "publication": r"\b(?:launch\w*|introduc\w+|releas\w+|unveil\w*|rolls? out|debut\w*|expands?|cria|criou|lan[çcz]\w+|publica\w+)\b|发布|推出|开源|上线",
    "financial": r"\b(?:earnings|revenues?|interest rates?|inflation|inflaci[oó]n|juros|receita)\b|财报|营收|利率|通胀",
    "security": r"\b(?:attacks?|strikes?|ceasefire|invasion|ataques?|bombarde\w+|cessar.fogo)\b|袭击|空袭|停火",
    "disaster": r"\b(?:earthquake|wildfire|flood\w*|cyclone|terremot\w+|inunda\w+)\b|地震|洪灾|台风",
    "leadership": r"\b(?:names|appoint\w+|resign\w*|nominat\w+|dimite|renuncia\w*)\b|辞职|获任|任命",
}
ACTION_RX = {key: re.compile(value, re.I) for key, value in ACTIONS.items()}
WIRE = re.compile(r"\b(Reuters|Associated Press|Agence France.Presse|Ag[eê]ncia Brasil|Agencia EFE|AFP)\b|新华社|美联社|路透社|法新社")
UPSTREAM = re.compile(r"\b(?:Defen[cs]e Forces|Border Guard)\b|\b[A-Za-z]+ authorities (?:said|confirmed)", re.I)
ATTRIBUTED = re.compile(
    r"according to|(?:the|a) (?:press release|statement)|seg[uú]n |de acordo com|em comunicado|"
    r"据.{0,14}(?:报道|公告|通报)|新闻稿|company said|company announced|(?:border guard|police|ministry|authorities|officials) (?:said|announced|reported)", re.I)
OWN_REPORTING = re.compile(r"told (?:us|the newsroom|reporters)\b|in an interview with|reporting from|"
                           r"prestou entrevista|en entrevista con|接受.{0,14}采访|记者在.{0,20}(?:目睹|现场)", re.I)
WORDS = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ][\w.-]{2,}|[\u3400-\u9fff]{2,}")
NUMBERS = re.compile(r"(?<![A-Za-z0-9])[0-9]+(?:[.,][0-9]+)*(?:\s?(?:%|km|MW|GB|kWh|billion|million|亿|万))?(?![A-Za-z0-9])", re.I)
CAPITAL = re.compile(r"\b(?:[A-Z][a-zÀ-ÖØ-öø-ÿ]+(?:\s+[A-Z][a-zÀ-ÖØ-öø-ÿ]+){1,3}|[A-Z][A-Z0-9.-]{2,})\b")
STOP = {"the", "and", "with", "from", "for", "this", "announces", "new", "news", "los", "las", "para",
        "com", "uma", "sobre", "that", "after", "says", "more", "will", "global"}
REGION = re.compile(r"United Nations|European Union|cross.border|international|G20|全球|联合国|欧盟|跨境|internacional", re.I)
CONTENTS = {"article", "main"}
SKIP = {"footer", "nav", "aside", "form", "noscript"}


def action_set(text):
    return {key for key, rule in ACTION_RX.items() if rule.search(text)}


NEGATION = re.compile(r"\b(?:did not|does not|has not|have not|never|denied|rejected|no longer|neg[oó]|n[aã]o)\b|未曾|尚未|否认|并未|没有", re.I)
STATUS = {
    "planned": re.compile(r"\b(?:may|could|plans? to|propos\w+|seeks? to|expected to|prev[êe]|planeja)\b|拟|计划|有望|预计", re.I),
    "in-progress": re.compile(r"\b(?:pending|under review|continues?|negotiat\w+)\b|进行中|仍在|尚待", re.I),
    "confirmed-action": re.compile(r"\b(?:signed|completed|published|launched|approved|occurred|acquired|released|announced|firmou)\b|已签署|发布了|正式批准|已完成|发生", re.I),
}
DATE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b|(?:20\d{2}年)?\d{1,2}月\d{1,2}日")


def phase(sentence):
    if NEGATION.search(sentence):
        return "denied-or-negated"
    return next((name for name, matcher in STATUS.items() if matcher.search(sentence)), "unknown")


def tokens(text):
    result = set()
    for item in WORDS.findall(text.lower()):
        if item in STOP:
            continue
        if re.fullmatch(r"[\u3400-\u9fff]+", item):
            result.update(item[i:i + 2] for i in range(len(item) - 1))
        else:
            result.add(item)
    return result


def numbers(text):
    return {item.casefold().replace(" ", "") for item in NUMBERS.findall(text)}


def similarity(first, second):
    left, right = tokens(first), tokens(second)
    return len(left & right) / len(left | right) if left and right else 0


def related(first, second, translator):
    first_title, second_title = first["title"], second["title"]
    actions = action_set(first_title) & action_set(second_title)
    if not actions:
        return 0
    # Numeric and location disagreements are safe reasons to keep separate clusters.
    # A shared recurring title template (e.g. a disaster feed) is not an event ID.
    first_numbers, second_numbers = numbers(first_title), numbers(second_title)
    if first_numbers and second_numbers and first_numbers != second_numbers:
        return 0
    if "disaster" in actions:
        place = r"\bin\s+(.+?)(?:\s+\d{1,2}/|[,.]|$)"
        places = [re.search(place, value, re.I) for value in (first_title, second_title)]
        if all(places) and places[0][1].casefold() != places[1][1].casefold():
            return 0
    direct = similarity(first["title"], second["title"])
    if first["language"] == second["language"] or direct >= .28:
        return direct
    left, right = (translator(row["title"], row["language"]) for row in (first, second))
    return max(direct, similarity(left, right)) if left and right else direct


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scope = 0
        self.skip = 0
        self.capture = None
        self.paragraphs = []
        self.structured = []
        self.links = []
        self.author = ""
        self.h1 = ""

    def handle_starttag(self, name, attrs):
        attr = dict(attrs)
        if name in CONTENTS:
            self.scope += 1
        if name in SKIP:
            self.skip += 1
        if (name == "p" and self.scope and not self.skip) or name == "h1":
            self.capture = name, []
        if name == "script" and attr.get("type") == "application/ld+json":
            self.capture = name, []
        if name == "meta" and attr.get("name") == "author":
            self.author = attr.get("content", "")
        if name == "a" and self.scope and not self.skip and attr.get("href"):
            self.links.append(attr["href"])

    def handle_data(self, data):
        if self.capture:
            self.capture[1].append(data)

    def handle_endtag(self, name):
        if name in CONTENTS:
            self.scope = max(0, self.scope - 1)
        if name in SKIP:
            self.skip = max(0, self.skip - 1)
        if not self.capture or name != self.capture[0]:
            return
        raw = "".join(self.capture[1])
        self.capture = None
        value = " ".join(raw.split())
        if name == "p" and len(value) >= 65:
            self.paragraphs.append(value)
        elif name == "h1" and not self.h1:
            self.h1 = value
        elif name == "script":
            try:
                self.structured.append(json.loads(raw))
            except ValueError:
                pass


def walked(value):
    if isinstance(value, list):
        for item in value:
            yield from walked(item)
    elif isinstance(value, dict):
        yield value
        yield from walked(value.get("@graph", []))


def extract(content):
    parser = ArticleParser()
    parser.feed(content)
    for tree in parser.structured:
        for node in walked(tree):
            body = node.get("articleBody")
            if isinstance(body, str) and len(body) >= 220:
                parser.paragraphs = [part.strip() for part in re.split(r"\n+", body) if len(part.strip()) >= 50]
                parser.h1 = node.get("headline") or parser.h1
                byline = node.get("author")
                if isinstance(byline, dict) and isinstance(byline.get("name"), str):
                    parser.author = byline["name"]
                break
    paragraphs = list(dict.fromkeys(parser.paragraphs))
    return parser.h1, paragraphs[:14], parser.author, parser.links


def host_of(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def readable_url(url, domains):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and host_of(url) in domains)


@dataclass
class Article:
    row: dict
    paragraphs: list
    lineage: str
    origin: str
    links: list


def article(row, domains):
    url = row["url"]
    if url.startswith("http://") and host_of(url) in {"gdacs.org", "africanews.com"}:
        url = "https://" + url[7:]
    try:
        if not readable_url(url, domains):
            return None, "地址不在公开新闻源范围"
        for _ in range(2):
            result = run(
                ["curl", "-qfsS", "--proto", "=https", "--compressed", "--connect-timeout", "4",
                 "--max-time", "8", "--max-filesize", "1500000", "--user-agent",
                 "Global-Intelligence-Radar/0.2", "--write-out", "\n%{http_code} %{redirect_url}", url],
                capture_output=True, timeout=10,
            )
            content, _, response = result.stdout.rpartition(b"\n")
            status, _, redirect = response.decode("utf-8", "replace").partition(" ")
            if not result.returncode and status == "200":
                break
            if not (status in {"301", "302", "307", "308"}
                    and readable_url(redirect, domains) and host_of(redirect) == host_of(url)):
                return None, f"正文不可用：HTTP {status or '?'} / curl {result.returncode}"
            url = redirect
        else:
            return None, "重定向未取得正文"
        if len(content) > 1_500_000:
            return None, "网页超出单次读取限制"
        headline, paragraphs, author, links = extract(content.decode("utf-8", errors="replace"))
    except (OSError, ValueError, TimeoutExpired) as error:
        return None, f"正文不可用：{type(error).__name__}"
    if len(" ".join(paragraphs)) < 320 or (headline and similarity(headline, row["title"]) < .18):
        return None, "正文不足或与新闻线索不一致"
    lead = " ".join(paragraphs[:3])
    attribution = WIRE.search(author + " " + lead)
    host = host_of(url)
    if attribution:
        name = (attribution[1] or attribution[0]).casefold()
        wire_names = {"美联社": "associated press", "路透社": "reuters", "法新社": "afp",
                      "agence france-presse": "afp", "agencia efe": "efe"}
        wire = wire_names.get(name, name)
        owner = {"reuters": "reuters.com", "associated press": "apnews.com", "afp": "afp.com",
                 "新华社": "news.cn", "efe": "efe.com"}.get(wire)
        lineage, origin = ("通讯社原稿" if host == owner else "转载或转述", wire)
    elif row["role"] in {"官方", "机构"}:
        lineage, origin = "机构一手", host
    elif UPSTREAM.search(lead):
        lineage, origin = "转述官方通报，未见独立采证", ""
    elif ATTRIBUTED.search(lead):
        lineage, origin = "共同引用风险", ""
    elif OWN_REPORTING.search(lead) and author:
        lineage, origin = "署名且有独立采证线索", host
    else:
        lineage, origin = "独立性未知", ""
    links = list(dict.fromkeys(urljoin(url, link) for link in links))
    return Article(row, paragraphs, lineage, origin, links), ""


def sentence_chunks(paragraphs):
    return [part.strip() for para in paragraphs
            for part in re.split(r"(?<=[。！？])\s*|(?<=[.!?])\s+", para)
            if 30 <= len(part.strip()) <= 350]


def extract_claims(report):
    """Preserve exact source statements and derive tentative fields for comparison."""
    title = report.row["title"]
    title_words = tokens(title)
    rows = []
    for sentence in sentence_chunks(report.paragraphs):
        action = action_set(sentence) & action_set(title)
        common = tokens(sentence) & title_words
        if len(common) < 2 or not action:
            continue
        rows.append(dict(action=sorted(action), wording=sentence, subjects=CAPITAL.findall(sentence),
                         numbers=sorted(numbers(sentence)), event_dates=DATE.findall(sentence),
                         phase=phase(sentence), url=report.row["url"],
                         published=report.row["published"], language=report.row["language"]))
    return rows[:6]


def compare(first, second, translated):
    if not first.origin or not second.origin or first.origin == second.origin:
        return [], "独立采证不足或同源"
    if "转载或转述" in (first.lineage, second.lineage):
        return [], "转载只能用于发现"
    item_a, item_b = extract_claims(first), extract_claims(second)
    confirmed = []
    conflicts = []
    for a in item_a:
        for b in item_b:
            if not set(a["action"]) & set(b["action"]):
                continue
            words_a, words_b = a["wording"], b["wording"]
            if a["language"] != b["language"]:
                words_a, words_b = translated(words_a, a["language"]), translated(words_b, b["language"])
            if not words_a or not words_b:
                continue
            left, right = tokens(words_a), tokens(words_b)
            if len(left & right) < 4 or similarity(words_a, words_b) < .58:
                continue
            subjects = set(a["subjects"]) & set(b["subjects"])
            if not subjects:
                continue  # Cross-language identities without a stable anchor remain unresolved.
            if a["phase"] == "unknown" or b["phase"] == "unknown":
                continue
            if (a["phase"] != b["phase"] or set(a["numbers"]) != set(b["numbers"])
                    or (a["event_dates"] and b["event_dates"] and set(a["event_dates"]) != set(b["event_dates"]))):
                conflicts.append((a, b))
                continue
            if SequenceMatcher(None, words_a, words_b).ratio() >= .97:
                continue  # Near-identical downstream wording does not establish independent reporting.
            confirmed.append({"subject": sorted(subjects), "action": sorted(set(a["action"]) & set(b["action"])),
                              "statements": [a, b]})
    if conflicts:
        return [], "对应断言的状态、数字或日期不一致，无法自动排除核心冲突"
    return confirmed, "" if confirmed else "未找到相互支持的核心断言"


def translation_cache(translator):
    return lru_cache(maxsize=1800)(translator)
