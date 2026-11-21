"""Version protection for the future long-lived SQLite state."""
import base64
from dataclasses import dataclass
import json
import re
from urllib.error import URLError
from uuid import uuid4

from state_roundtrip import MAX_BUNDLE, REPO, api, pack, restore

PATH = "/contents/state/sqlite-current-v1.json"


class VersionConflict(Exception):
    """A different version is current; discard the local working copy."""


class SaveUncertain(Exception):
    """The write could not be confirmed; do not retry automatically."""


class WriteRejected(Exception):
    """The repository explicitly rejected the write."""


@dataclass(frozen=True)
class Snapshot:
    version: str
    bundle: bytes

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{40}", self.version):
            raise ValueError("Invalid snapshot version.")


def load(repository, database):
    """Return the exact revision paired with the database just restored."""
    snapshot = repository.read()
    restore(snapshot.bundle, database)
    return snapshot


def initialize(repository, database):
    """Create the long-lived file once, with no expected prior version."""
    return _write(repository, database, None)


def save(repository, database, base):
    """Conditionally write against the revision captured at load time."""
    if not isinstance(base, Snapshot):
        raise TypeError("An expected Snapshot is required for an update.")
    return _write(repository, database, base)


def _write(repository, database, base):
    expected = base.version if base is not None else None
    bundle, info, _ = pack(database)
    if base is not None and json.loads(base.bundle)["signature"] == info:
        if repository.read().version != expected:
            raise VersionConflict("Snapshot changed; no update was written.")
        return base
    content = json.loads(bundle)
    # Keep each successful update unique even if the database reverts to older bytes.
    content["revision"] = uuid4().hex
    bundle = json.dumps(content, separators=(",", ":")).encode()
    if len(bundle) > MAX_BUNDLE:
        raise ValueError("Versioned snapshot exceeds upload limit.")
    try:
        return repository.replace(bundle, expected)
    except (OSError, SaveUncertain) as error:
        # A lost response is not a reason to issue a second write.
        try:
            current = repository.read()
        except (LookupError, OSError, SystemExit, WriteRejected, ValueError):
            raise SaveUncertain("Write result unknown; no retry was attempted.") from error
        if current.bundle == bundle:
            return current
        raise SaveUncertain("Write result could not be confirmed; no retry was attempted.") from error


class GitHubStateRepository:
    """GitHub Contents adapter. No calls are made until a method is invoked."""

    def __init__(self, request=api):
        self.request = request

    @property
    def endpoint(self):
        return PATH

    def check_repository(self):
        info = self.request("")
        if (info.get("full_name", "").lower() != REPO or info.get("private") is not True
                or info.get("default_branch") != "main"):
            raise WriteRejected("Unexpected state repository or visibility.")

    def read(self):
        self.check_repository()
        result = self.request(self.endpoint + "?ref=main")
        if result.get("encoding") != "base64" or result.get("size", MAX_BUNDLE + 1) > MAX_BUNDLE:
            raise ValueError("Invalid or oversized snapshot response.")
        return Snapshot(result["sha"], base64.b64decode("".join(result["content"].split()), validate=True))

    def replace(self, bundle, expected):
        self.check_repository()
        payload = {"message": "Save verified Radar SQLite state",
                   "branch": "main", "content": base64.b64encode(bundle).decode("ascii")}
        if expected is not None:
            payload["sha"] = expected
        try:
            result = self.request(self.endpoint, payload)
        except SystemExit as error:
            # The benchmark helper reports HTTP status only, with no credential or body.
            match = re.fullmatch(r"GitHub request refused: HTTP (\d+)\.", str(error))
            status = int(match[1]) if match else None
            if status == 409:
                raise VersionConflict("State repository rejected a conflicting write.") from None
            if status == 422:
                current = self.read()
                if current.version != expected:
                    raise VersionConflict("Snapshot changed; no update was written.") from None
                raise WriteRejected("GitHub rejected the snapshot update (HTTP 422).") from None
            if status is not None and status >= 500:
                raise SaveUncertain("GitHub did not confirm the write.") from None
            raise
        except URLError as error:
            raise SaveUncertain("GitHub did not confirm the write.") from error
        try:
            return Snapshot(result["content"]["sha"], bundle)
        except (KeyError, TypeError, ValueError):
            raise SaveUncertain("GitHub returned no usable snapshot version.") from None
