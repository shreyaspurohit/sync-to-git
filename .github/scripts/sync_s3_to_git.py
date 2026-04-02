from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


ENDPOINT = os.environ.get("S3_ENDPOINT_URL")
BUCKET = os.environ["S3_BUCKET_NAME"]
VERBOSE = os.environ.get("VERBOSE_LOG", "false").lower() in ("1", "true", "yes", "on")

SYNC_DIR = Path(os.environ.get("SYNC_DIR", "data"))
MANIFEST_PATH = Path(os.environ.get("SYNC_MANIFEST_PATH", "manifest.json"))
STEP_SUMMARY_PATH = os.environ.get("GITHUB_STEP_SUMMARY")


@dataclass(frozen=True)
class ObjectMeta:
    key: str
    size: int
    etag: str
    last_modified: str

    @staticmethod
    def from_s3(obj: dict) -> "ObjectMeta":
        return ObjectMeta(
            key=obj["Key"],
            size=obj["Size"],
            etag=obj["ETag"],
            last_modified=obj["LastModified"],
        )

    def to_dict(self) -> dict:
        return {
            "Key": self.key,
            "Size": self.size,
            "ETag": self.etag,
            "LastModified": self.last_modified,
        }

    def diff(self, old: "ObjectMeta | None") -> str:
        if old is None:
            return "new object"

        diffs = []
        if self.size != old.size:
            diffs.append(f"size {old.size} -> {self.size}")
        if self.etag != old.etag:
            diffs.append(f"etag {old.etag} -> {self.etag}")
        if self.last_modified != old.last_modified:
            diffs.append(f"last_modified {old.last_modified} -> {self.last_modified}")

        return ", ".join(diffs) if diffs else "no change"


class SyncLog:
    @staticmethod
    def info(msg: str) -> None:
        print(msg, flush=True)

    @staticmethod
    def verbose(msg: str) -> None:
        if VERBOSE:
            SyncLog.info(msg)

    @staticmethod
    def notice(msg: str) -> None:
        SyncLog.info(f"::notice::{msg}")

    @staticmethod
    def warning(msg: str) -> None:
        SyncLog.info(f"::warning::{msg}")

    @staticmethod
    def write_summary(lines: list[str]) -> None:
        if not STEP_SUMMARY_PATH:
            return
        with open(STEP_SUMMARY_PATH, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


class S3SyncSource:
    @staticmethod
    def with_endpoint(cmd: list[str]) -> list[str]:
        if ENDPOINT:
            return [*cmd, "--endpoint-url", ENDPOINT]
        return cmd

    @staticmethod
    def run(cmd: list[str]) -> str:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return result.stdout

    @staticmethod
    def list_all_objects() -> list[ObjectMeta]:
        token = None
        items: list[ObjectMeta] = []
        page = 0

        while True:
            page += 1
            cmd = S3SyncSource.with_endpoint(
                [
                    "aws",
                    "s3api",
                    "list-objects-v2",
                    "--bucket",
                    BUCKET,
                    "--output",
                    "json",
                ]
            )
            if token:
                cmd += ["--continuation-token", token]

            data = json.loads(S3SyncSource.run(cmd))
            SyncLog.verbose(f"Raw response page {page}: {json.dumps(data)[:2000]}")
            batch = data.get("Contents", [])
            SyncLog.info(f"Listed page {page}: {len(batch)} objects")

            for obj in batch:
                items.append(ObjectMeta.from_s3(obj))

            if not data.get("IsTruncated"):
                break
            token = data.get("NextContinuationToken")

        items.sort(key=lambda item: item.key)
        return items

    @staticmethod
    def download_object(key: str) -> None:
        destination = SYNC_DIR / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        SyncLog.verbose(f"Downloading object body: {key}")

        subprocess.run(
            S3SyncSource.with_endpoint(
                [
                    "aws",
                    "s3",
                    "cp",
                    f"s3://{BUCKET}/{key}",
                    str(destination),
                    "--only-show-errors",
                ]
            ),
            check=True,
        )


class ManifestStore:
    @staticmethod
    def load() -> dict[str, ObjectMeta]:
        if not MANIFEST_PATH.exists():
            SyncLog.info("No previous manifest found; treating this as first sync run.")
            return {}

        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        items = [ObjectMeta.from_s3(item) for item in raw]
        SyncLog.info(f"Loaded previous manifest with {len(items)} objects")
        return {item.key: item for item in items}

    @staticmethod
    def save(items: list[ObjectMeta]) -> None:
        serializable = [item.to_dict() for item in items]
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(serializable, indent=2) + "\n", encoding="utf-8")
        SyncLog.info(f"Saved manifest with {len(items)} objects")


class LocalMirror:
    @staticmethod
    def delete_missing(current_keys: set[str]) -> list[str]:
        removed_files = []

        for path in SYNC_DIR.rglob("*"):
            if path.is_file():
                relative_path = path.relative_to(SYNC_DIR).as_posix()
                if relative_path not in current_keys:
                    removed_files.append(relative_path)
                    path.unlink()

        for relative_path in removed_files:
            SyncLog.verbose(f"Deleted local file removed from bucket: {relative_path}")

        for path in sorted(SYNC_DIR.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

        return removed_files


class SummaryFormatter:
    @staticmethod
    def truncate_items(items: list[str], limit: int = 25) -> list[str]:
        if len(items) <= limit:
            return items
        return items[:limit] + [f"... and {len(items) - limit} more"]

    @staticmethod
    def build_summary(
        *,
        bucket: str,
        endpoint: str | None,
        sync_dir: Path,
        manifest_path: Path,
        new_map: dict[str, ObjectMeta],
        old_map: dict[str, ObjectMeta],
        new_items: list[ObjectMeta],
        added: list[str],
        changed: list[str],
        removed: list[str],
        to_download: list[str],
        removed_local: list[str],
    ) -> list[str]:
        summary_lines = [
            "## Sync-to-Git Summary",
            "",
            f"- Bucket: `{bucket}`",
            f"- Endpoint: `{endpoint or 'AWS default endpoint'}`",
            f"- Sync directory: `{sync_dir}`",
            f"- Manifest path: `{manifest_path}`",
            f"- Total objects: **{len(new_items)}**",
            f"- New: **{len(added)}**",
            f"- Changed: **{len(changed)}**",
            f"- Removed: **{len(removed)}**",
            f"- Downloaded: **{len(to_download)}**",
            f"- Deleted locally: **{len(removed_local)}**",
            f"- Verbose: **{VERBOSE}**",
            "",
        ]

        if added:
            summary_lines.extend(
                [
                    "### New objects",
                    *[f"- `{item}`" for item in SummaryFormatter.truncate_items(added)],
                    "",
                ]
            )

        if changed:
            summary_lines.append("### Changed objects")
            for key in SummaryFormatter.truncate_items(changed):
                if key.startswith("... and "):
                    summary_lines.append(f"- {key}")
                else:
                    reason = new_map[key].diff(old_map.get(key))
                    summary_lines.append(f"- `{key}` - {reason}")
            summary_lines.append("")

        if removed:
            summary_lines.extend(
                [
                    "### Removed objects",
                    *[f"- `{item}`" for item in SummaryFormatter.truncate_items(removed)],
                    "",
                ]
            )

        return summary_lines


def main() -> None:
    SYNC_DIR.mkdir(exist_ok=True)

    new_items = S3SyncSource.list_all_objects()
    old_map = ManifestStore.load()
    new_map = {item.key: item for item in new_items}

    added = []
    changed = []
    unchanged = []

    for key, item in new_map.items():
        old = old_map.get(key)
        if old is None:
            added.append(key)
        elif old != item:
            changed.append(key)
        else:
            unchanged.append(key)

    removed = sorted(set(old_map.keys()) - set(new_map.keys()))

    SyncLog.info("")
    SyncLog.info("==== Sync comparison summary ====")
    SyncLog.info(f"Total objects in bucket: {len(new_items)}")
    SyncLog.info(f"New objects: {len(added)}")
    SyncLog.info(f"Changed objects: {len(changed)}")
    SyncLog.info(f"Removed objects: {len(removed)}")
    SyncLog.info(f"Unchanged objects: {len(unchanged)}")
    SyncLog.info(f"Verbose logging: {VERBOSE} with 25 limit in each list")
    SyncLog.info("")

    if VERBOSE and added:
        SyncLog.info("---- New objects ----")
        for key in added:
            SyncLog.info(f"[NEW] {key}")

    if VERBOSE and changed:
        SyncLog.info("---- Changed objects ----")
        for key in changed:
            reason = new_map[key].diff(old_map.get(key))
            SyncLog.info(f"[CHANGED] {key} :: {reason}")

    if VERBOSE and removed:
        SyncLog.info("---- Removed objects ----")
        for key in removed:
            SyncLog.info(f"[REMOVED] {key}")

    to_download = added + changed
    total = len(to_download)

    if total:
        SyncLog.info("")
        SyncLog.info("---- Download phase ----")
        for index, key in enumerate(to_download, start=1):
            reason = new_map[key].diff(old_map.get(key))
            SyncLog.info(f"[{index}/{total}] processing {key} :: {reason}")
            S3SyncSource.download_object(key)
    else:
        SyncLog.info("No new or changed objects to download.")

    removed_local = LocalMirror.delete_missing(set(new_map.keys()))
    ManifestStore.save(new_items)

    if added:
        SyncLog.notice(f"{len(added)} new objects synced")
    if changed:
        SyncLog.notice(f"{len(changed)} changed objects synced")
    if removed_local:
        SyncLog.warning(f"{len(removed_local)} local synced objects removed")

    SyncLog.write_summary(
        SummaryFormatter.build_summary(
            bucket=BUCKET,
            endpoint=ENDPOINT,
            sync_dir=SYNC_DIR,
            manifest_path=MANIFEST_PATH,
            new_map=new_map,
            old_map=old_map,
            new_items=new_items,
            added=added,
            changed=changed,
            removed=removed,
            to_download=to_download,
            removed_local=removed_local,
        )
    )

    SyncLog.info("")
    SyncLog.info("==== Final result ====")
    SyncLog.info(f"Downloaded new objects: {len(added)}")
    SyncLog.info(f"Downloaded changed objects: {len(changed)}")
    SyncLog.info(f"Deleted local removed objects: {len(removed_local)}")
    SyncLog.info("Sync mirror update complete.")


if __name__ == "__main__":
    main()
