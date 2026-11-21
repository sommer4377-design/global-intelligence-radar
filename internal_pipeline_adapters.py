"""Read-only news adapters and file-only release/notification rehearsal."""
from concurrent.futures import ThreadPoolExecutor
from html import escape
import json
from pathlib import Path
from urllib.parse import urlsplit

from internal_pipeline import atomic, digest, encoded, result, stamp

SITE = "https://sommer4377-design.github.io/global-intelligence-radar/"


def store_once(path, content):
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Immutable rehearsal artifact differs.")
    else:
        atomic(path, content)


def verify_archive(base):
    manifest = json.loads((base / "manifest.json").read_text())
    if set(manifest["files"]) != {"index.html"}:
        raise ValueError("Unexpected archived file.")
    for name, checksum in manifest["files"].items():
        if digest((base / name).read_bytes()) != checksum:
            raise ValueError("Archived bytes differ from manifest.")
    return manifest


def rollback(engine):
    with engine.locked():
        pointer = engine.root / "simulation" / "deployment.json"
        record = json.loads(pointer.read_text())
        if not record.get("previous"):
            raise ValueError("No previous simulated deployment exists.")
        previous = record["previous"]
        if previous != str(Path("archive") / previous.split("/")[-1]):
            raise ValueError("Invalid rollback target.")
        verify_archive(engine.root / previous)
        atomic(pointer, encoded(dict(current=previous, previous=record["current"], simulated=True)))


class Adapters:
    def __init__(self, fail=None):
        self.fail = fail
        self._translator = None

    def execute(self, p, stage):
        if self.fail == stage:
            raise RuntimeError("测试注入故障")
        return getattr(self, stage)(p)

    def translator(self, p):
        if self._translator is None:
            from automatic import Translator
            from intelligence.blind_evidence import translation_cache
            path = p.settings["models"]
            self._translator = translation_cache(Translator(Path(path) if path else None).translate)
        return self._translator

    def collect(self, p):
        if p.mode == "mock":
            return result(2, synthetic=True, entries=[{"id":"fixture"}, {"id":"fixture"}])
        from intelligence.discovery import collect, focus
        if p.settings["capture"]:
            rows = json.loads(Path(p.settings["capture"]).read_text())["results"]
        else:
            rows = collect()
        ok = sum(bool(articles) for _, articles, _ in rows)
        if not ok:
            raise RuntimeError("所有来源均未取得可解析新闻")
        entries = focus(rows, p.start, p.cutoff)
        return result(len(entries), feeds_ok=ok, feeds_total=len(rows), entries=entries,
                      faults=[{"source":source,"error":error} for source, _, error in rows if error])

    def identify(self, p):
        entries = p.payload("collect")["entries"]
        if p.mode == "mock":
            return result(len(entries), synthetic=True, groups=[[row] for row in entries])
        from intelligence.evidence_trial import distinct, priority
        rows = distinct(entries)
        return result(len(rows), groups=[[row] for row in sorted(rows, key=priority, reverse=True)])

    def deduplicate(self, p):
        entries = p.payload("collect")["entries"]
        if p.mode == "mock":
            return result(1, synthetic=True, groups=[[entries[0]]], duplicate_free=True)
        from intelligence.blind import event_groups
        from intelligence.evidence_trial import priority
        rows = [group[0] for group in p.payload("identify")["groups"]]
        groups = sorted(event_groups(rows, self.translator(p)), key=lambda g: priority(g[0]), reverse=True)
        return result(len(groups), groups=groups)

    def search(self, p):
        groups = p.payload("deduplicate")["groups"]
        if p.mode == "mock":
            return result(1, synthetic=True, review=[{"fixture":True,"verified":True}])
        if not groups:
            return result(skipped="无候选事件", review=[])
        from intelligence.evidence_trial import build_catalog, examine
        all_rows = [row for group in groups for row in group]
        catalog, translator = build_catalog(all_rows), self.translator(p)
        selected = groups[:p.settings["limit"]]
        with ThreadPoolExecutor(max_workers=3) as pool:
            # Existing examine performs retrieval and claim comparison together.
            reviewed = list(pool.map(lambda group: examine(group, catalog, all_rows,
                           translator, p.start, p.cutoff, False, []), selected))
        return result(len(reviewed), review=reviewed)

    def verify(self, p):
        review = p.payload("search")["review"]
        verified = [row for row in review if row["verified"]]
        return result(len(verified), verified=verified, held=len(review)-len(verified))

    def write(self, p):
        verified = p.payload("verify")["verified"]
        if not verified:
            return result(skipped="没有通过双源核实的候选", drafts=[])
        if p.mode == "mock":
            return result(1, synthetic=True, drafts=[{"title":"模拟条目","draft":"模拟正文，不是新闻"}])
        if not p.settings["runtime"]:
            return result(skipped="未提供现有本地模型和运行时", drafts=[])
        from intelligence.local_writer import inference
        drafts = []
        for candidate in verified[:3]:
            fragments = dict.fromkeys(item["wording"] for claim in candidate["packet"]
                                      for item in claim["evidence"])
            facts = {f"A{i}":sentence for i,sentence in enumerate(fragments, 1)}
            response = inference(Path(p.settings["runtime"]), Path(p.settings["model"]), facts)
            if response.get("error") or response.get("returncode") != 0:
                raise RuntimeError("现有模型未成功执行")
            drafts.append(dict(title=candidate["title"], **response))
        return result(len(drafts), drafts=drafts)

    def quality(self, p):
        drafts = p.payload("write")["drafts"]
        if p.mode == "mock":
            return result(len(drafts), quality="SYNTHETIC_PASS", synthetic=True)
        if not drafts:
            return result(quality="BLOCKED_NO_QUALIFIED_STORIES")
        if any(row["problems"] for row in drafts):
            return result(quality="BLOCKED_FACT_OR_FORMAT_CHECK")
        return result(quality="REVIEW_REQUIRED")

    def render(self, p):
        from intelligence.page import STYLE
        quality = p.payload("quality")["quality"]
        marker = "模拟数据 · 仅验证流程" if p.mode == "mock" else "真实模块审计 · 不可发布"
        body = "".join(f'<li>{escape(row["title"])} · {escape(row["reason"])}</li>'
                       for row in p.payload("search").get("review", []) if "title" in row)
        text = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{marker}</title><style>{STYLE}</style>'
                f'<main><h1>{marker}</h1><p>{p.batch} · {quality}</p><ul>{body}</ul></main>')
        path = p.root / "runs" / p.batch / "index.html"
        atomic(path, text.encode())
        return result(1, sha=digest(text.encode()))

    def archive(self, p):
        page = (p.root / "runs" / p.batch / "index.html").read_bytes()
        if digest(page) != p.payload("render")["sha"]:
            raise ValueError("Rendered page changed before archive.")
        location = Path("archive") / p.batch
        base = p.root / location
        metadata = dict(batch=p.batch, version=digest(page), quality=p.payload("quality")["quality"],
                        files={"index.html":digest(page)})
        store_once(base / "index.html", page)
        store_once(base / "manifest.json", encoded(metadata))
        verify_archive(base)
        return result(1, path=str(location), version=metadata["version"])

    def prepare(self, p):
        manifest = verify_archive(p.root / p.payload("archive")["path"])
        # Existing content policy does not provide a production pass for the auto-generated drafts.
        if p.mode != "mock" or not p.settings["dry_run"]:
            return result(skipped="质量未满足正式发布条件；内部模式不部署", ready=False)
        groups = p.payload("deduplicate")["groups"]
        ids = [group[0]["id"] for group in groups]
        if (manifest["quality"] != "SYNTHETIC_PASS" or len(set(ids)) != len(ids)
                or not p.payload("verify")["verified"]):
            return result(skipped="模拟发布条件不足", ready=False)
        return result(1, ready=False, simulation_ready=True, version=manifest["version"])

    def deploy(self, p):
        if not p.payload("prepare").get("simulation_ready"):
            return result(skipped="没有满足条件的发布包")
        path = p.payload("archive")["path"]
        manifest = verify_archive(p.root / path)
        pointer = p.root / "simulation" / "deployment.json"
        record = json.loads(pointer.read_text()) if pointer.exists() else {}
        if record.get("current") != path:
            atomic(pointer, encoded(dict(current=path, previous=record.get("current"), simulated=True)))
        if json.loads(pointer.read_text())["current"] != path:
            raise RuntimeError("模拟部署回读失败")
        return result(1, receipt={"verified":True,"public_verified":False,"simulated":True,
                                   "archive":path,"version":manifest["version"]})

    def notify(self, p):
        receipt = p.payload("deploy").get("receipt", {})
        if not (p.mode == "mock" and receipt.get("simulated") and receipt.get("verified")):
            return result(skipped="没有通过验证的部署回执", notification="NOT_SENT")
        future_link = SITE + f"brief-{p.batch}.html"
        notice = {"channel":"serverchan-turbo", "title":f"Radar 中文简报｜{p.batch}",
                  "body":f"[阅读完整简报]({future_link})", "simulated":True}
        with p.db:
            saved = p.db.execute("SELECT status FROM pipeline_notice WHERE batch=?", (p.batch,)).fetchone()
            if saved:
                return result(0, notification=saved[0])
            p.db.execute("INSERT INTO pipeline_notice VALUES (?,?,?,?)",
                         (p.batch,"SIMULATED_SENT",json.dumps(notice, ensure_ascii=False),stamp()))
        return result(1, notification="SIMULATED_SENT")
