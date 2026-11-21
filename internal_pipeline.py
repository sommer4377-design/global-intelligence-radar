"""Isolated, resumable production rehearsal. No remote deployment or notification exists here."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import NamedTemporaryFile
import time
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
STAGES = ("collect", "identify", "deduplicate", "search", "verify", "write", "quality",
          "render", "archive", "prepare", "deploy", "notify")
TERMINAL = {"COMPLETED", "SKIPPED"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_batch(
 id TEXT PRIMARY KEY, mode TEXT NOT NULL, start TEXT NOT NULL, cutoff TEXT NOT NULL,
 created TEXT NOT NULL, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pipeline_stage(
 batch TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 started TEXT, ended TEXT, seconds REAL, input_count INTEGER, output_count INTEGER, error TEXT,
 retriable INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(batch,name));
CREATE TABLE IF NOT EXISTS pipeline_attempt(
 batch TEXT, stage TEXT, attempt INTEGER, status TEXT, started TEXT, ended TEXT,
 seconds REAL, error TEXT, PRIMARY KEY(batch,stage,attempt));
CREATE TABLE IF NOT EXISTS pipeline_notice (
 batch TEXT PRIMARY KEY, status TEXT, content TEXT, updated TEXT);
"""


def stamp():
    return datetime.now(timezone.utc).isoformat()


def slot_id(now):
    if now.utcoffset() is None:
        raise ValueError("Scheduler requires a timezone-aware time.")
    local = now.astimezone(BEIJING)
    if local.hour < 8:
        local -= timedelta(days=1)
        hour = 20
    else:
        hour = 8 if local.hour < 20 else 20
    return f"{local:%Y-%m-%d}-{hour:02d}"


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        pending = Path(temporary.name)
        temporary.write(content)
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def digest(data):
    return sha256(data).hexdigest()


def result(count=0, *, skipped="", **fields):
    return dict(count=count, skipped=skipped, **fields)


class Pipeline:
    def __init__(self, root, batch, mode, settings, now=None):
        if mode not in {"real", "mock"}:
            raise ValueError("Use real or mock mode.")
        root = Path(root)
        if root.is_symlink():
            raise ValueError("The internal test root cannot be a symlink.")
        self.root, self.batch, self.mode = root.resolve(), batch, mode
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-(?:08|20)", batch):
            raise ValueError("Use a Beijing edition ID YYYY-MM-DD-08 or YYYY-MM-DD-20.")
        self.cutoff = datetime.strptime(batch, "%Y-%m-%d-%H").replace(tzinfo=BEIJING)
        self.start = self.cutoff - timedelta(hours=12)
        self.settings = settings
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.root/"runs", self.root/"archive", self.root/"simulation",
                     self.root/"runs"/batch, self.root/"archive"/batch):
            if path.is_symlink():
                raise ValueError("Unsafe internal artifact directory.")
        with self.locked(), closing(self.connect()) as db, db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO pipeline_batch VALUES (?,?,?,?,?,?)", (
                batch, mode, self.start.isoformat(), self.cutoff.isoformat(),
                (now or datetime.now(timezone.utc)).isoformat(), json.dumps(settings, sort_keys=True)))
            current = db.execute("SELECT mode,settings FROM pipeline_batch WHERE id=?", (batch,)).fetchone()
            if current != (mode, json.dumps(settings, sort_keys=True)):
                raise ValueError("Batch is already bound to different inputs or mode.")
            db.executemany("INSERT OR IGNORE INTO pipeline_stage(batch,name,status) VALUES (?,?,'PENDING')",
                           [(batch, stage) for stage in STAGES])

    @contextmanager
    def locked(self):
        lock = self.root / "pipeline.lock"
        if lock.is_symlink():
            raise ValueError("Unsafe internal lock path.")
        with lock.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def connect(self):
        path = self.root / "internal-pipeline.sqlite3"
        if path.is_symlink():
            raise ValueError("Unsafe internal SQLite path.")
        return sqlite3.connect(path)

    def state(self, stage):
        row = self.db.execute(
            "SELECT status,payload FROM pipeline_stage WHERE batch=? AND name=?", (self.batch, stage)).fetchone()
        return row[0], json.loads(row[1])

    def previous(self, stage):
        pos = STAGES.index(stage)
        return self.state(STAGES[pos-1])[1] if pos else {}

    def payload(self, stage):
        return self.state(stage)[1]

    def recover(self):
        ended = stamp()
        with self.db:
            self.db.execute("UPDATE pipeline_attempt SET status='FAILED',ended=?,error=? "
                            "WHERE batch=? AND status='RUNNING'", (ended, "中断：进程未完成", self.batch))
            self.db.execute("UPDATE pipeline_stage SET status='FAILED',ended=?,error=?,retriable=1 "
                            "WHERE batch=? AND status='RUNNING'", (ended, "中断：进程未完成", self.batch))

    def stage(self, name, adapters):
        inputs = self.previous(name).get("count", 0)
        started, timer = stamp(), time.monotonic()
        with self.db:
            self.db.execute(
                "UPDATE pipeline_stage SET status='RUNNING',attempts=attempts+1,started=?,ended=NULL,"
                "seconds=NULL,input_count=?,output_count=NULL,error=NULL,retriable=0 WHERE batch=? AND name=?",
                (started, inputs, self.batch, name))
            attempt = self.db.execute("SELECT attempts FROM pipeline_stage WHERE batch=? AND name=?",
                                      (self.batch, name)).fetchone()[0]
            self.db.execute("INSERT INTO pipeline_attempt(batch,stage,attempt,status,started) "
                            "VALUES (?,?,?,'RUNNING',?)", (self.batch, name, attempt, started))
        try:
            data = adapters.execute(self, name)
            status, error = ("SKIPPED", data["skipped"]) if data["skipped"] else ("COMPLETED", "")
        except Exception as failure:
            data, status, error = {}, "FAILED", f"{type(failure).__name__}: {failure}"
        ended, seconds = stamp(), round(time.monotonic()-timer, 4)
        with self.db:
            self.db.execute("UPDATE pipeline_stage SET status=?,ended=?,seconds=?,output_count=?,error=?,"
                            "retriable=?,payload=? WHERE batch=? AND name=?",
                            (status, ended, seconds, data.get("count", 0), error, status == "FAILED",
                             json.dumps(data, ensure_ascii=False), self.batch, name))
            self.db.execute("UPDATE pipeline_attempt SET status=?,ended=?,seconds=?,error=? "
                            "WHERE batch=? AND stage=? AND attempt=?",
                            (status, ended, seconds, error, self.batch, name, attempt))
        return status

    def execute(self, adapters, only=None, resume=False):
        with self.locked(), closing(self.connect()) as self.db:
            try:
                if resume:
                    self.recover()
                stages = (only,) if only else STAGES
                for name in stages:
                    status, _ = self.state(name)
                    if status in TERMINAL:
                        continue
                    if status == "RUNNING" or (status == "FAILED" and not resume):
                        return 1
                    prior = STAGES[:STAGES.index(name)]
                    if any(self.state(step)[0] not in TERMINAL for step in prior):
                        return 2
                    if self.stage(name, adapters) == "FAILED":
                        return 1
                return 0
            finally:
                self.report()

    def report(self):
        self.db.row_factory = sqlite3.Row
        try:
            stages = [dict(row) for row in self.db.execute(
                "SELECT * FROM pipeline_stage WHERE batch=? ORDER BY rowid", (self.batch,))]
            attempts = [dict(row) for row in self.db.execute(
                "SELECT * FROM pipeline_attempt WHERE batch=? ORDER BY rowid", (self.batch,))]
            created = self.db.execute("SELECT created FROM pipeline_batch WHERE id=?", (self.batch,)).fetchone()[0]
        finally:
            self.db.row_factory = None
        payloads = {stage["name"]:json.loads(stage.pop("payload")) for stage in stages}
        statuses = {row["status"] for row in stages}
        report = dict(batch=self.batch, mode=self.mode, start=self.start.isoformat(),
                      cutoff=self.cutoff.isoformat(), generated=created,
                      technical=("FAILED" if "FAILED" in statuses else "INTERRUPTED" if "RUNNING" in statuses
                                 else "INCOMPLETE" if "PENDING" in statuses else "COMPLETED"),
                      quality=payloads["quality"].get("quality", "NOT_EVALUATED"),
                      publication_ready=payloads["prepare"].get("ready", False),
                      archive=payloads["archive"].get("path"),
                      deploy=payloads["deploy"].get("receipt"),
                      notification=payloads["notify"].get("notification", "NOT_SENT"),
                      counts={k:payloads[k].get("count", 0) for k in
                              ("collect","identify","deduplicate","search","verify","write")},
                      source_counts={key:payloads["collect"].get(key) for key in ("feeds_ok","feeds_total")},
                      source_faults=payloads["collect"].get("faults", []),
                      evidence_holds=[{key:item[key] for key in ("title","reason","seconds")}
                                      for item in payloads["search"].get("review", [])
                                      if not item.get("verified") and "title" in item],
                      total_seconds=round(sum(row["seconds"] or 0 for row in attempts), 4),
                      stages=stages, attempts=attempts)
        base = self.root / "runs" / self.batch
        atomic(base / "report.json", encoded(report))
        label = "模拟计数，仅验证流程" if self.mode == "mock" else "真实模块计数"
        lines = [f"# 内部批次 {self.batch}", "", f"{label} · 技术状态 {report['technical']} · "
                 f"内容状态 {report['quality']} · 发布准备 {report['publication_ready']}",
                 f"采集 {report['counts']['collect']} · 候选 {report['counts']['identify']} · 双源通过 {report['counts']['verify']} · 中文草稿 {report['counts']['write']}",
                 f"归档 {report['archive'] or '暂无'} · 通知 {report['notification']} · 尝试耗时 {report['total_seconds']} 秒",
                 "", "| 阶段 | 状态 | 输入 | 输出 | 秒 | 尝试 | 说明 |",
                 "|---|---|---:|---:|---:|---:|---|"]
        for stage in stages:
            error = (stage["error"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {stage['name']} | {stage['status']} | {stage['input_count'] or 0} | "
                         f"{stage['output_count'] or 0} | {stage['seconds'] or 0} | {stage['attempts']} | {error} |")
        atomic(base / "report.md", ("\n".join(lines)+"\n").encode())


def main():
    from internal_pipeline_adapters import Adapters, rollback
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mode", choices=("real","mock"), default="real")
    parser.add_argument("--batch")
    parser.add_argument("--as-of", type=datetime.fromisoformat, default=datetime.now(BEIJING))
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Only simulate deployment; never access remote write APIs.")
    parser.add_argument("--rollback", action="store_true", help="Restore the previous simulated deployment.")
    parser.add_argument("--fail-stage", choices=STAGES, help="Inject a failure in mock mode only.")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--limit", type=int, choices=range(1,33), default=4)
    args = parser.parse_args()
    if args.fail_stage and args.mode != "mock":
        parser.error("--fail-stage requires --mode mock")
    if bool(args.runtime) != bool(args.model):
        parser.error("--runtime and --model must be specified together.")
    settings = {key: str(getattr(args,key).resolve()) if getattr(args,key) else None
                for key in ("capture","models","runtime","model")}
    settings.update(limit=args.limit, dry_run=args.dry_run)
    try:
        engine = Pipeline(args.root, args.batch or slot_id(args.as_of), args.mode, settings)
        if args.rollback:
            if args.mode != "mock" or not args.dry_run:
                parser.error("--rollback requires mock dry-run.")
            rollback(engine)
            return 0
        code = engine.execute(Adapters(args.fail_stage), args.stage, args.resume)
        print(engine.root / "runs" / engine.batch / "report.md")
        return code
    except (OSError, ValueError, RuntimeError) as error:
        print(f"内部流水线停止：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
