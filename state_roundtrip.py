"""Two-run state benchmark, restricted to the designated private repository."""
import argparse
import base64
import gzip
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, Request

from state_transfer import signature, transfer

REPO = "sommer4377-design/global-intelligence-radar-state"
HOST = "https://api.github.com/repos/" + REPO
FILE = "/contents/benchmark/sqlite-snapshot-v1.json"
MAX_BUNDLE = 900 * 1024
MAX_DB = 16 * 1024 * 1024
TABLES = ("articles", "revisions", "health", "signals", "findings", "finding_history")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(path, payload=None):
    token = os.environ.get("RADAR_STATE_TOKEN")
    if not token:
        raise SystemExit("RADAR_STATE_TOKEN is missing.")
    request = Request(HOST + path, data=json.dumps(payload).encode() if payload is not None else None,
                      method="PUT" if payload is not None else "GET",
                      headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                               "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            return json.loads(response.read(2 * MAX_BUNDLE))
    except HTTPError as error:
        raise SystemExit(f"GitHub request refused: HTTP {error.code}.") from None


def counts(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("SQLite quick_check failed.")
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}


def pack(path):
    with TemporaryDirectory() as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        info = transfer(path, snapshot, "backup")
        if info["bytes"] > MAX_DB:
            raise ValueError("SQLite snapshot exceeds size limit.")
        tables = counts(snapshot)
        if not tables["articles"]:
            raise ValueError("SQLite snapshot contains no news.")
        encoded = base64.b64encode(gzip.compress(snapshot.read_bytes(), mtime=0)).decode("ascii")
        bundle = json.dumps({"version": 1, "signature": info, "tables": tables, "sqlite": encoded},
                            separators=(",", ":")).encode()
        if len(bundle) > MAX_BUNDLE:
            raise ValueError("Compressed snapshot exceeds upload limit.")
        return bundle, info, tables


def restore(bundle, target):
    if len(bundle) > MAX_BUNDLE:
        raise ValueError("Bundle exceeds restore limit.")
    data = json.loads(bundle)
    if data["version"] != 1:
        raise ValueError("Unsupported snapshot format.")
    with TemporaryDirectory() as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(data["sqlite"], validate=True))) as stream:
            buffer = stream.read(MAX_DB + 1)
        if len(buffer) > MAX_DB:
            raise ValueError("Snapshot exceeds restore limit.")
        snapshot.write_bytes(buffer)
        if signature(snapshot) != data["signature"] or counts(snapshot) != data["tables"]:
            raise ValueError("Snapshot signature or counts differ.")
        snapshot.with_suffix(".manifest.json").write_text(json.dumps(data["signature"]))
        transfer(snapshot, target, "restore")
    return data["signature"], data["tables"]


def verify_retained(before, after):
    previous, current = counts(before), counts(after)
    with sqlite3.connect(after.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("ATTACH DATABASE ? AS old", (before.resolve().as_uri() + "?mode=ro",))
        lost = db.execute("SELECT count(*) FROM old.articles WHERE url NOT IN (SELECT url FROM main.articles)").fetchone()[0]
    if lost:
        raise ValueError(f"{lost} historical URLs disappeared.")
    print("Historical URLs retained:", previous["articles"], "new:", current["articles"] - previous["articles"])


def reject_corruption(path):
    bundle, _, _ = pack(path)
    damaged = json.loads(bundle)
    damaged["signature"]["sha256"] = "0" * 64
    with TemporaryDirectory() as directory:
        destination = Path(directory) / "protected.sqlite3"
        transfer(path, destination, "backup")
        initial = signature(destination)
        try:
            restore(json.dumps(damaged).encode(), destination)
        except ValueError:
            if signature(destination) == initial:
                print("Invalid digest rejected; existing local database preserved.")
                return
    raise ValueError("Corruption was not rejected safely.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("seed", "restore", "verify"))
    parser.add_argument("database", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--before", type=Path)
    args = parser.parse_args()
    if args.operation == "verify":
        if not args.before:
            parser.error("--before is required for verify")
        verify_retained(args.before, args.database)
        reject_corruption(args.before)
        return
    details = api("")
    if details.get("full_name", "").lower() != REPO or details.get("private") is not True or details.get("default_branch") != "main":
        raise SystemExit("State repository identity, visibility or branch check failed.")
    if args.operation == "seed":
        bundle, info, tables = pack(args.database)
        # Omitting sha makes this a create-only write; an existing file is never overwritten.
        result = api(FILE, {"message": "Save first Radar SQLite test snapshot", "branch": "main",
                            "content": base64.b64encode(bundle).decode("ascii")})
        print("State commit:", result["commit"]["sha"])
    else:
        if not args.commit or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
            parser.error("--commit must contain a complete 40-character commit SHA")
        result = api(FILE + "?ref=" + args.commit)
        info, tables = restore(base64.b64decode(result["content"]), args.database)
    print("SQLite SHA-256:", info["sha256"], "table counts:", json.dumps(tables, sort_keys=True))


if __name__ == "__main__":
    main()
