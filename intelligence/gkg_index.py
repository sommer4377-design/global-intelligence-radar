"""Read bounded public GDELT GKG archive slices as a discovery index."""
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
import re
from subprocess import run
from zipfile import ZipFile, BadZipFile

from .blind_evidence import host_of, similarity, tokens

GKG = "https://data.gdeltproject.org/gdeltv2/"
SOURCES = {
    "apnews.com": ("Associated Press", "en"),
    "reuters.com": ("Reuters", "en"),
    "bbc.com": ("BBC", "en"),
    "abcnews.com": ("ABC News", "en"),
    "abcnews.go.com": ("ABC News", "en"),
    "cbc.ca": ("CBC", "en"),
    "thehindu.com": ("The Hindu", "en"),
    "helsinkitimes.com": ("Helsinki Times", "en"),
    "kyivpost.com": ("Kyiv Post", "en"),
    "thelocal.fr": ("The Local France", "en"),
    "theguardian.com": ("The Guardian", "en"),
    "elpais.com": ("El País", "es"),
    "efe.com": ("Agencia EFE", "es"),
    "agenciabrasil.ebc.com.br": ("Agência Brasil", "pt"),
    "africanews.com": ("Africanews", "en"),
}
TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>")
DATE = re.compile(r"<PAGE_PRECISEPUBTIMESTAMP>(\d{14})</PAGE_PRECISEPUBTIMESTAMP>")
MAX_COMPRESSED = 9_000_000
MAX_EXPANDED = 20_000_000


def download(stamp, path):
    datetime.strptime(stamp, "%Y%m%d%H%M%S")
    result = run(["curl", "-qfsS", "--proto", "=https", "--connect-timeout", "6", "--max-time", "35",
                  "--max-filesize", str(MAX_COMPRESSED), GKG + stamp + ".gkg.csv.zip",
                  "--output", str(path)],
                 capture_output=True, timeout=38)
    if result.returncode:
        raise OSError(f"GKG HTTP 读取失败，curl {result.returncode}")


def read_archive(path, cutoff):
    rows = []
    if path.stat().st_size > MAX_COMPRESSED:
        raise ValueError("GKG 文件过大")
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].file_size > MAX_EXPANDED:
            raise ValueError("GKG 压缩内容不符合限制")
        with archive.open(entries[0]) as content:
            for line in content:
                fields = line.decode("utf-8", "replace").rstrip("\n").split("\t")
                if len(fields) != 27 or not fields[4].startswith("https://"):
                    continue
                matched = TITLE.search(fields[26])
                if not matched:
                    continue
                try:
                    observed = datetime.strptime(fields[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                    publication = DATE.search(fields[26])
                    published = (datetime.strptime(publication[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                                 if publication else None)
                except ValueError:
                    continue
                if observed > cutoff or (published and published > cutoff):
                    continue
                host = host_of(fields[4])
                source, language = SOURCES.get(host, ("", ""))
                rows.append(dict(title=unescape(matched[1])[:260], source=source, url=fields[4],
                                 language=language, role="媒体", provider="GDELT GKG",
                                 published=published.isoformat() if published else "归档仅有发现时间"))
    return rows


def shortlist(seed, rows, translator):
    query = seed["title"]
    zh = None
    ranked = []
    for row in rows:
        if not row["source"] or host_of(seed["url"]) == host_of(row["url"]):
            continue
        score = similarity(query, row["title"])
        if score < .25 and seed["language"] != row["language"] and tokens(query) & tokens(row["title"]):
            zh = zh or translator(query, seed["language"])
            translated = translator(row["title"], row["language"])
            if zh and translated:
                score = max(score, similarity(zh, translated))
        if score >= .26:
            ranked.append((score, row))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in ranked[:3]]
