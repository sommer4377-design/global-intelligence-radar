"""Manual GitHub test isolated to a run-specific path and synthetic data."""
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from state_roundtrip import TABLES, reject_corruption
from versioned_snapshots import GitHubStateRepository, VersionConflict, initialize, load, save

RUN_REPO = "sommer4377-design/global-intelligence-radar"


class GitHubValidationRepository(GitHubStateRepository):
    """Only accepts Actions-generated numeric run identifiers."""

    def __init__(self, run_id, attempt, **kwargs):
        super().__init__(**kwargs)
        if not all(re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in (run_id, attempt)):
            raise ValueError("Invalid GitHub run identifier or attempt.")
        self._endpoint = f"/contents/benchmark/version-protection/{run_id}-{attempt}/snapshot.json"

    @property
    def endpoint(self):
        return self._endpoint


def insert_article(database, key):
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO articles(url) VALUES (?)", (key,))


def run_validation(repository):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        seed = root / "seed.sqlite3"
        with closing(sqlite3.connect(seed)) as db, db:
            for table in TABLES:
                db.execute(f"CREATE TABLE {table}(url TEXT PRIMARY KEY)")
        insert_article(seed, "synthetic-original")
        initial = initialize(repository, seed)
        fresh_path, stale_path = root / "fresh.sqlite3", root / "stale.sqlite3"
        fresh_base, stale_base = load(repository, fresh_path), load(repository, stale_path)
        if fresh_base != initial or stale_base != initial:
            raise RuntimeError("Restored writers did not share the initialized snapshot.")
        insert_article(fresh_path, "synthetic-winning-write")
        winner = save(repository, fresh_path, fresh_base)
        if winner.version == initial.version:
            raise RuntimeError("Saving new content did not advance the snapshot version.")
        insert_article(stale_path, "synthetic-stale-write")
        try:
            save(repository, stale_path, stale_base)
        except VersionConflict:
            pass
        else:
            raise RuntimeError("Stale write unexpectedly succeeded on the isolated test path.")
        restored = root / "restored.sqlite3"
        remote = load(repository, restored)
        if remote != winner:
            raise RuntimeError("The winning snapshot changed after conflict.")
        with closing(sqlite3.connect(restored)) as db:
            urls = db.execute("SELECT url FROM articles ORDER BY url").fetchall()
        if urls != [("synthetic-original",), ("synthetic-winning-write",)]:
            raise RuntimeError("The winning snapshot did not contain the expected rows.")
        reject_corruption(restored)
        print("Initial save, stale write rejection, winning snapshot restore and local corruption test: PASS")


def main():
    if (os.environ.get("GITHUB_REPOSITORY") != RUN_REPO
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise SystemExit("Only the designated repository's manual main workflow may use this entry point.")
    repository = GitHubValidationRepository(os.environ.get("GITHUB_RUN_ID", ""),
                                           os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    print("Isolated synthetic test path:", repository.endpoint.removeprefix("/contents/"))
    run_validation(repository)


if __name__ == "__main__":
    main()
