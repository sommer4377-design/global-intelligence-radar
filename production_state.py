"""Private production-state handoff for an explicitly authorized Actions run."""
import argparse
import base64
from functools import partial
import json
from pathlib import Path
import re

from state_roundtrip import FILE, api, pack, restore
from versioned_snapshots import GitHubStateRepository, Snapshot, WriteRejected, initialize, load, save

MAX_BUNDLE = 8 * 1024**2
MAX_DATABASE = 96 * 1024**2
SOFT_REPOSITORY_LIMIT_KIB = 768 * 1024
pack_state = partial(pack, max_db=MAX_DATABASE, max_bundle=MAX_BUNDLE)
restore_state = partial(restore, max_db=MAX_DATABASE, max_bundle=MAX_BUNDLE)


def request(path, payload=None):
    accept = "application/vnd.github.object+json" if path.startswith("/contents/") and payload is None else "application/vnd.github+json"
    return api(path, payload, accept=accept, response_limit=16 * 1024**2)


class ProductionRepository(GitHubStateRepository):
    def __init__(self, requester=request):
        super().__init__(requester)

    def read(self):
        self.check_repository()
        metadata = self.request(self.endpoint + "?ref=main")
        if metadata.get("type") != "file" or not 0 < metadata.get("size", 0) <= MAX_BUNDLE:
            raise ValueError("Invalid or oversized production snapshot.")
        version = metadata.get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", version):
            raise ValueError("Invalid production snapshot blob version.")
        # Fetch immutable contents by SHA, even when larger than 1 MiB.
        blob = self.request("/git/blobs/" + version)
        if blob.get("sha") != version or blob.get("encoding") != "base64":
            raise ValueError("Invalid Git blob response.")
        bundle = base64.b64decode("".join(blob["content"].split()), validate=True)
        if len(bundle) != metadata["size"]:
            raise ValueError("Production snapshot size differs from metadata.")
        return Snapshot(version, bundle)

    def replace(self, bundle, expected):
        self.check_repository()
        usage = self.request("").get("size")
        if type(usage) is not int or usage >= SOFT_REPOSITORY_LIMIT_KIB:
            raise WriteRejected("Private repository size limit reached or unavailable.")
        return super().replace(bundle, expected)


def prepare(directory, seed_commit=None, repository=None):
    repository = repository or ProductionRepository()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = directory / "radar.sqlite3"
    if seed_commit:
        if not re.fullmatch(r"[0-9a-f]{40}", seed_commit):
            raise ValueError("Bootstrap requires the exact seed commit SHA.")
        repository.check_repository()
        # Only an explicit bootstrap may use the benchmark snapshot.
        try:
            repository.request(repository.endpoint + "?ref=main")
        except SystemExit as error:
            if str(error) != "GitHub request refused: HTTP 404.":
                raise
        else:
            raise WriteRejected("Production state already exists; use update.")
        seed = repository.request(FILE + "?ref=" + seed_commit)
        restore(base64.b64decode("".join(seed["content"].split()), validate=True), database)
        (directory / "bootstrap").touch(exist_ok=False)
        print("Pinned benchmark seed restored for bootstrap.")
        return
    base = load(repository, database, unpack=restore_state)
    (directory / "base.bundle").write_bytes(base.bundle)
    (directory / "base.sha").write_text(base.version, encoding="ascii")
    print("Production snapshot restored and verified.")


def persist(directory, repository=None):
    repository = repository or ProductionRepository()
    database = directory / "radar.sqlite3"
    marker = directory / "bootstrap"
    if marker.is_file():
        if (directory / "base.sha").exists():
            raise ValueError("Mixed bootstrap and update state.")
        result = initialize(repository, database, packer=pack_state, max_bundle=MAX_BUNDLE)
    else:
        base = Snapshot((directory / "base.sha").read_text(encoding="ascii"),
                        (directory / "base.bundle").read_bytes())
        result = save(repository, database, base, packer=pack_state, max_bundle=MAX_BUNDLE)
    print(f"Private state verified: {len(result.bundle)} / {MAX_BUNDLE} bytes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "persist"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--seed-commit")
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare(args.directory, args.seed_commit)
    elif args.seed_commit is not None:
        parser.error("--seed-commit is only available for prepare")
    else:
        persist(args.directory)


if __name__ == "__main__":
    main()
