"""Stage only verified, generated HTML for GitHub Pages."""
import argparse
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import ipaddress
from pathlib import Path
import re
from urllib.parse import parse_qsl, unquote, urlsplit

PAGE_NAME = re.compile(r"brief-\d{4}-\d{2}-\d{2}-(08|20)\.html")
MAX_SITE_BYTES = 16 * 1024**2
SENSITIVE = re.compile(
    r"file://|/(?:Users|home|private|Volumes|tmp)/|[A-Z]:\\Users\\|"
    r"(?<![A-Za-z0-9_-])(?:github_pat_[\w]+|gh[pousr]_[A-Za-z0-9_]{12,}|sk-(?:proj-)?[A-Za-z0-9_-]{15,})|"
    r"AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|RADAR_STATE_TOKEN|"
    r"(?:read|write)\s+\S+\.sqlite3|benchmark/sqlite-snapshot-v1\.json", re.I)
SECRET_KEYS = {"token", "access_token", "api_key", "apikey", "auth", "signature", "secret",
               "password", "x-amz-credential", "x-amz-signature"}
HEALTH = re.compile(r"<details><summary>本轮采集状态</summary>.*?</details>", re.S)


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = set()
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"script", "iframe", "base", "form", "object", "embed", "svg"}:
            raise ValueError(f"Unexpected active element: {tag}")
        if "id" in values:
            self.anchors.add(values["id"])
        for key, value in attrs:
            if key.startswith("on") or key in {"srcdoc", "http-equiv", "style"}:
                raise ValueError(f"Unexpected active attribute: {key}")
            if key in {"href", "src", "action", "srcset", "xlink:href"}:
                self.links.append(value or "")


def validate_link(raw, name, pages):
    if "\\" in raw or any(ord(char) < 32 for char in raw):
        raise ValueError("Unsafe characters in a link.")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        hostname = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not hostname or "." not in hostname
                or parsed.username is not None or parsed.password is not None
                or hostname.endswith((".local", ".localhost", ".internal"))):
            raise ValueError("Only public HTTP(S) URLs may be published.")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("Non-public address in an outgoing link.")
        if any(key.casefold() in SECRET_KEYS for key, _ in parse_qsl(parsed.query)):
            raise ValueError("Credential-like query parameter in an outgoing link.")
        return
    target = unquote(parsed.path) or name
    if target not in pages or (parsed.fragment and unquote(parsed.fragment) not in pages[target].anchors):
        raise ValueError(f"Broken internal link in {name}.")


def stage(source, destination):
    paths = [path for path in source.glob("brief-*.html") if PAGE_NAME.fullmatch(path.name)]
    paths.append(source / "index.html")
    if len(paths) == 1:
        raise ValueError("No official editions found.")
    pages, content = {}, {}
    total = 0
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SITE_BYTES:
            raise ValueError("Missing, symbolic or oversized page.")
        original = path.read_text(encoding="utf-8")
        header = original.partition("</header>")[0]
        if ("真实来源自动简报" not in header or "截点前预览" in header
                or "演示数据" in original or "合成样本" in original):
            raise ValueError("Only official non-demo editions may be staged.")
        if path.name != "index.html":
            datetime.strptime(path.name, "brief-%Y-%m-%d-%H.html")
        public = HEALTH.sub("", original)
        if SENSITIVE.search(unquote(unescape(public))):
            raise ValueError("Sensitive-looking content in generated page.")
        total += len(public.encode())
        if total > MAX_SITE_BYTES:
            raise ValueError("Public site exceeds the size limit.")
        page = PageLinks()
        page.feed(public)
        pages[path.name] = page
        content[path.name] = public
    for name, page in pages.items():
        for link in page.links:
            validate_link(link, name, pages)
    destination.mkdir(parents=True, exist_ok=False)
    for name, public in content.items():
        (destination / name).write_text(public, encoding="utf-8")
    print(f"Public package checked: {len(pages)} HTML pages, {total} bytes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    stage(args.source, args.destination)
