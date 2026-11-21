"""将 SQLite 状态交给另一独立运行环境，校验失败时保留目标库。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile


def check(db):
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ValueError("SQLite 快速完整性检查失败")


def signature(path):
    with path.open("rb") as stream:
        digest = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def transfer(source, destination, mode):
    if source.resolve() == destination.resolve() or not source.is_file():
        raise ValueError("输入缺失或与目标路径相同")
    manifest = (destination if mode == "backup" else source).with_suffix(".manifest.json")
    if mode == "restore" and signature(source) != json.loads(manifest.read_text(encoding="utf-8")):
        raise ValueError("状态文件校验失败，现有数据库保留")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        staged = Path(temp.name)
    try:
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as current:
            with sqlite3.connect(staged) as replacement:
                current.backup(replacement)
                check(replacement)
        if mode == "backup":
            info = signature(staged)
            # 校验清单最后发布；任一中断均会安全拒绝不一致的组合。
            staged.replace(destination)
            pending = manifest.with_suffix(".tmp")
            pending.write_text(json.dumps(info), encoding="utf-8")
            pending.replace(manifest)
        else:
            staged.replace(destination)
        return signature(destination)
    finally:
        staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backup", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(transfer(args.source, args.destination, args.mode)))


if __name__ == "__main__":
    main()
