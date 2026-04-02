import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.abc import Loader
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "sync_s3_to_git.py"


FAKE_AWS_SCRIPT = """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

FIXTURE_DIR = Path(os.environ["FAKE_AWS_FIXTURE_DIR"])
OBJECTS_DIR = FIXTURE_DIR / "objects"
COMMAND_LOG = os.environ.get("FAKE_AWS_COMMAND_LOG")


def log_command(args):
    if not COMMAND_LOG:
        return
    with open(COMMAND_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")


def list_objects(args):
    token = None
    if "--continuation-token" in args:
        token = args[args.index("--continuation-token") + 1]

    pages = json.loads((FIXTURE_DIR / "list_pages.json").read_text())
    if token is None:
        page = pages[0]
    else:
        page = next(item for item in pages if item["token"] == token)

    print(json.dumps(page["response"]))


def copy_object(args):
    src = args[0]
    dest = Path(args[1])
    if not src.startswith("s3://"):
        raise SystemExit(f"unexpected source: {src}")

    key = src.split("/", 3)[3]
    source_path = OBJECTS_DIR / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, dest)


def main():
    args = sys.argv[1:]
    log_command(args)
    if args[:2] == ["s3api", "list-objects-v2"]:
        list_objects(args)
        return
    if args[:2] == ["s3", "cp"]:
        copy_object(args[2:])
        return
    raise SystemExit(f"unsupported aws invocation: {args}")


if __name__ == "__main__":
    main()
"""


def load_sync_module() -> ModuleType:
    with mock.patch.dict(os.environ, {"S3_BUCKET_NAME": "unit-test-bucket"}, clear=False):
        spec = spec_from_file_location("sync_s3_to_git_under_test", SCRIPT_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load module spec from {SCRIPT_PATH}")
        loader: Loader = spec.loader
        module = module_from_spec(spec)
        sys.modules[spec.name] = module
        loader.exec_module(module)
        return module


sync_module = load_sync_module()


class HelperClassTests(unittest.TestCase):
    def test_object_meta_diff_reports_field_changes(self):
        old = sync_module.ObjectMeta("note.md", 1, '"a"', "2026-03-30T00:00:00Z")
        new = sync_module.ObjectMeta("note.md", 2, '"b"', "2026-03-31T00:00:00Z")

        self.assertEqual(new.diff(None), "new object")
        self.assertEqual(
            new.diff(old),
            'size 1 -> 2, etag "a" -> "b", last_modified 2026-03-30T00:00:00Z -> 2026-03-31T00:00:00Z',
        )

    def test_s3_sync_source_with_endpoint_appends_endpoint(self):
        with mock.patch.object(sync_module, "ENDPOINT", "https://endpoint.example"):
            command = sync_module.S3SyncSource.with_endpoint(["aws", "s3api"])
        self.assertEqual(command, ["aws", "s3api", "--endpoint-url", "https://endpoint.example"])

    def test_s3_sync_source_with_endpoint_leaves_command_unchanged_when_unset(self):
        with mock.patch.object(sync_module, "ENDPOINT", None):
            command = sync_module.S3SyncSource.with_endpoint(["aws", "s3api"])
        self.assertEqual(command, ["aws", "s3api"])

    def test_manifest_store_load_and_save_round_trip(self):
        with tempfile.TemporaryDirectory() as tempdir:
            manifest_path = Path(tempdir) / "nested" / "manifest.json"
            items = [sync_module.ObjectMeta("a.txt", 1, '"etag"', "2026-03-31T00:00:00Z")]

            with mock.patch.object(sync_module, "MANIFEST_PATH", manifest_path):
                sync_module.ManifestStore.save(items)
                loaded = sync_module.ManifestStore.load()

            self.assertEqual(list(loaded.keys()), ["a.txt"])
            self.assertEqual(loaded["a.txt"], items[0])

    def test_local_mirror_delete_missing_removes_files_and_prunes_directories(self):
        with tempfile.TemporaryDirectory() as tempdir:
            sync_dir = Path(tempdir) / "data"
            keep_file = sync_dir / "keep.txt"
            remove_file = sync_dir / "nested" / "remove.txt"
            keep_file.parent.mkdir(parents=True, exist_ok=True)
            remove_file.parent.mkdir(parents=True, exist_ok=True)
            keep_file.write_text("keep", encoding="utf-8")
            remove_file.write_text("remove", encoding="utf-8")

            with mock.patch.object(sync_module, "SYNC_DIR", sync_dir):
                removed = sync_module.LocalMirror.delete_missing({"keep.txt"})

            self.assertEqual(removed, ["nested/remove.txt"])
            self.assertTrue(keep_file.exists())
            self.assertFalse(remove_file.exists())
            self.assertFalse((sync_dir / "nested").exists())

    def test_summary_formatter_truncates_and_builds_summary(self):
        old = sync_module.ObjectMeta("changed.txt", 1, '"old"', "2026-03-30T00:00:00Z")
        new = sync_module.ObjectMeta("changed.txt", 2, '"new"', "2026-03-31T00:00:00Z")

        self.assertEqual(
            sync_module.SummaryFormatter.truncate_items(["a", "b", "c"], limit=2),
            ["a", "b", "... and 1 more"],
        )

        with mock.patch.object(sync_module, "VERBOSE", True):
            summary = sync_module.SummaryFormatter.build_summary(
                bucket="bucket",
                endpoint=None,
                sync_dir=Path("data"),
                manifest_path=Path("manifest.json"),
                new_map={"changed.txt": new},
                old_map={"changed.txt": old},
                new_items=[new],
                added=[],
                changed=["changed.txt"],
                removed=["removed.txt"],
                to_download=["changed.txt"],
                removed_local=["removed.txt"],
            )

        summary_text = "\n".join(summary)
        self.assertIn("Endpoint: `AWS default endpoint`", summary_text)
        self.assertIn("- `changed.txt` - size 1 -> 2, etag \"old\" -> \"new\", last_modified 2026-03-30T00:00:00Z -> 2026-03-31T00:00:00Z", summary_text)
        self.assertIn("### Removed objects", summary_text)


class SyncS3ToGitTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.fixture_dir = self.root / "fixtures"
        self.fixture_dir.mkdir()
        (self.fixture_dir / "objects").mkdir()

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()

        fake_aws = self.bin_dir / "aws"
        fake_aws.write_text(FAKE_AWS_SCRIPT, encoding="utf-8")
        fake_aws.chmod(fake_aws.stat().st_mode | stat.S_IXUSR)

        shutil.copy2(SCRIPT_PATH, self.root / "sync_s3_to_git.py")

        self.summary_path = self.root / "step_summary.md"
        self.command_log_path = self.root / "aws_commands.jsonl"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "S3_ENDPOINT_URL": "https://example.invalid",
            "S3_BUCKET_NAME": "test-bucket",
            "FAKE_AWS_FIXTURE_DIR": str(self.fixture_dir),
            "FAKE_AWS_COMMAND_LOG": str(self.command_log_path),
            "GITHUB_STEP_SUMMARY": str(self.summary_path),
            "SYNC_DIR": "data",
            "SYNC_MANIFEST_PATH": "manifest.json",
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def write_fixture(self, key, content):
        path = self.fixture_dir / "objects" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_pages(self, pages):
        payload = [{"token": token, "response": response} for token, response in pages]
        (self.fixture_dir / "list_pages.json").write_text(json.dumps(payload), encoding="utf-8")

    def run_script(self, verbose=False):
        env = dict(self.env)
        if verbose:
            env["VERBOSE_LOG"] = "true"
        else:
            env.pop("VERBOSE_LOG", None)

        return subprocess.run(
            ["python3", "sync_s3_to_git.py"],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def read_command_log(self):
        if not self.command_log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.command_log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_first_run_downloads_all_objects_and_writes_manifest(self):
        self.write_fixture("note-1.md", "note one\n")
        self.write_fixture("nested/blob.bin", "blob data\n")
        self.write_pages(
            [
                (
                    None,
                    {
                        "IsTruncated": True,
                        "NextContinuationToken": "page-2",
                        "Contents": [
                            {
                                "Key": "note-1.md",
                                "Size": 9,
                                "ETag": '"etag-1"',
                                "LastModified": "2026-03-31T00:00:00Z",
                            }
                        ],
                    },
                ),
                (
                    "page-2",
                    {
                        "IsTruncated": False,
                        "Contents": [
                            {
                                "Key": "nested/blob.bin",
                                "Size": 10,
                                "ETag": '"etag-2"',
                                "LastModified": "2026-03-31T00:01:00Z",
                            }
                        ],
                    },
                ),
            ]
        )

        result = self.run_script()

        self.assertIn("No previous manifest found", result.stdout)
        self.assertIn("New objects: 2", result.stdout)
        self.assertTrue((self.root / "data" / "note-1.md").exists())
        self.assertTrue((self.root / "data" / "nested" / "blob.bin").exists())

        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["Key"] for item in manifest], ["nested/blob.bin", "note-1.md"])

        summary = self.summary_path.read_text(encoding="utf-8")
        self.assertIn("Total objects: **2**", summary)
        self.assertIn("Downloaded: **2**", summary)

    def test_incremental_run_downloads_changes_and_deletes_removed_files(self):
        old_manifest = [
            {
                "Key": "changed.md",
                "Size": 3,
                "ETag": '"old-changed"',
                "LastModified": "2026-03-30T00:00:00Z",
            },
            {
                "Key": "removed.md",
                "Size": 4,
                "ETag": '"old-removed"',
                "LastModified": "2026-03-30T00:05:00Z",
            },
            {
                "Key": "unchanged.md",
                "Size": 8,
                "ETag": '"same"',
                "LastModified": "2026-03-30T00:10:00Z",
            },
        ]
        (self.root / "manifest.json").write_text(
            json.dumps(old_manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        data_dir = self.root / "data"
        data_dir.mkdir()
        (data_dir / "changed.md").write_text("old\n", encoding="utf-8")
        (data_dir / "removed.md").write_text("remove me\n", encoding="utf-8")
        (data_dir / "unchanged.md").write_text("stay-put", encoding="utf-8")

        self.write_fixture("changed.md", "new body\n")
        self.write_fixture("new/note.md", "brand new\n")
        self.write_pages(
            [
                (
                    None,
                    {
                        "IsTruncated": False,
                        "Contents": [
                            {
                                "Key": "changed.md",
                                "Size": 9,
                                "ETag": '"new-changed"',
                                "LastModified": "2026-03-31T00:00:00Z",
                            },
                            {
                                "Key": "new/note.md",
                                "Size": 10,
                                "ETag": '"new-note"',
                                "LastModified": "2026-03-31T00:03:00Z",
                            },
                            {
                                "Key": "unchanged.md",
                                "Size": 8,
                                "ETag": '"same"',
                                "LastModified": "2026-03-30T00:10:00Z",
                            },
                        ],
                    },
                )
            ]
        )

        result = self.run_script(verbose=True)

        self.assertIn("Changed objects: 1", result.stdout)
        self.assertIn("Removed objects: 1", result.stdout)
        self.assertIn("[CHANGED] changed.md", result.stdout)
        self.assertEqual((data_dir / "changed.md").read_text(encoding="utf-8"), "new body\n")
        self.assertEqual((data_dir / "new" / "note.md").read_text(encoding="utf-8"), "brand new\n")
        self.assertFalse((data_dir / "removed.md").exists())
        self.assertEqual((data_dir / "unchanged.md").read_text(encoding="utf-8"), "stay-put")

        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["Key"] for item in manifest],
            ["changed.md", "new/note.md", "unchanged.md"],
        )

        summary = self.summary_path.read_text(encoding="utf-8")
        self.assertIn("New: **1**", summary)
        self.assertIn("Changed: **1**", summary)
        self.assertIn("Removed: **1**", summary)
        self.assertIn("Deleted locally: **1**", summary)

    def test_no_change_run_skips_downloads_and_uses_default_aws_endpoint_when_unset(self):
        old_manifest = [
            {
                "Key": "unchanged.md",
                "Size": 8,
                "ETag": '"same"',
                "LastModified": "2026-03-30T00:10:00Z",
            }
        ]
        (self.root / "manifest.json").write_text(
            json.dumps(old_manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        data_dir = self.root / "data"
        data_dir.mkdir()
        (data_dir / "unchanged.md").write_text("stay-put", encoding="utf-8")

        self.write_pages(
            [
                (
                    None,
                    {
                        "IsTruncated": False,
                        "Contents": [
                            {
                                "Key": "unchanged.md",
                                "Size": 8,
                                "ETag": '"same"',
                                "LastModified": "2026-03-30T00:10:00Z",
                            }
                        ],
                    },
                )
            ]
        )

        self.env.pop("S3_ENDPOINT_URL", None)
        result = self.run_script()

        self.assertIn("Changed objects: 0", result.stdout)
        self.assertIn("No new or changed objects to download.", result.stdout)
        self.assertEqual((data_dir / "unchanged.md").read_text(encoding="utf-8"), "stay-put")

        commands = self.read_command_log()
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:4], ["s3api", "list-objects-v2", "--bucket", "test-bucket"])
        self.assertNotIn("--endpoint-url", commands[0])

        summary = self.summary_path.read_text(encoding="utf-8")
        self.assertIn("Endpoint: `AWS default endpoint`", summary)
        self.assertIn("Downloaded: **0**", summary)
        self.assertIn("Deleted locally: **0**", summary)

    def test_custom_sync_dir_and_manifest_path_are_honored(self):
        self.write_fixture("folder/note.md", "custom path\n")
        self.write_pages(
            [
                (
                    None,
                    {
                        "IsTruncated": False,
                        "Contents": [
                            {
                                "Key": "folder/note.md",
                                "Size": 12,
                                "ETag": '"etag-custom"',
                                "LastModified": "2026-03-31T00:00:00Z",
                            }
                        ],
                    },
                )
            ]
        )

        self.env["SYNC_DIR"] = "snapshots"
        self.env["SYNC_MANIFEST_PATH"] = "state/s3-manifest.json"
        result = self.run_script()

        self.assertIn("Saved manifest with 1 objects", result.stdout)
        self.assertTrue((self.root / "snapshots" / "folder" / "note.md").exists())
        self.assertTrue((self.root / "state" / "s3-manifest.json").exists())

        summary = self.summary_path.read_text(encoding="utf-8")
        self.assertIn("Sync directory: `snapshots`", summary)
        self.assertIn("Manifest path: `state/s3-manifest.json`", summary)

    def test_summary_truncates_long_new_and_removed_lists(self):
        old_manifest = [
            {
                "Key": f"removed-{index:02d}.txt",
                "Size": 1,
                "ETag": f'"old-{index:02d}"',
                "LastModified": "2026-03-30T00:00:00Z",
            }
            for index in range(27)
        ]
        (self.root / "manifest.json").write_text(
            json.dumps(old_manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        data_dir = self.root / "data"
        data_dir.mkdir()
        for index in range(27):
            (data_dir / f"removed-{index:02d}.txt").write_text("x", encoding="utf-8")
            self.write_fixture(f"new-{index:02d}.txt", "n",)

        self.write_pages(
            [
                (
                    None,
                    {
                        "IsTruncated": False,
                        "Contents": [
                            {
                                "Key": f"new-{index:02d}.txt",
                                "Size": 1,
                                "ETag": f'"new-{index:02d}"',
                                "LastModified": "2026-03-31T00:00:00Z",
                            }
                            for index in range(27)
                        ],
                    },
                )
            ]
        )

        self.run_script()

        summary = self.summary_path.read_text(encoding="utf-8")
        self.assertIn("New: **27**", summary)
        self.assertIn("Removed: **27**", summary)
        self.assertIn("... and 2 more", summary)
        self.assertFalse((data_dir / "removed-00.txt").exists())


if __name__ == "__main__":
    unittest.main()
