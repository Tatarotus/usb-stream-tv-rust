"""End-to-end staging package: export -> template -> manifest -> validation."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ['cc', 'mkntfs', 'ntfsinfo', 'ntfsls', 'ntfscat']


def export_config():
    return {
        "schema_version": 1,
        "generation": 2,
        "output_profile": "1080p30",
        "identities": [
            {"identity": "sbt", "category": "Abertos", "filename": "SBT.trp",
             "status": "approved", "quality_status": "native_1080",
             "chosen": {"channel_id": "207198"}, "favorite_ids": ["207198"],
             "candidates_tested": [{"channel_id": "207198"}], "rejected": []},
            {"identity": "record", "category": "Abertos", "filename": "Record.trp",
             "status": "approved", "quality_status": "native_1080",
             "chosen": {"channel_id": "214640"}, "favorite_ids": ["214640"],
             "candidates_tested": [{"channel_id": "214640"}], "rejected": []},
            {"identity": "cnn-brasil", "category": "Noticias", "filename": "CNN Brasil.trp",
             "status": "approved", "quality_status": "native_1080",
             "chosen": {"channel_id": "206087"}, "favorite_ids": ["206087"],
             "candidates_tested": [{"channel_id": "206087"}], "rejected": []},
        ],
    }


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "NTFS tools required")
class StagingPackageTests(unittest.TestCase):
    def test_package_binds_export_layout_manifest_and_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export = root / "export.json"
            export.write_text(json.dumps(export_config()))
            staging = root / "staging"
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_live_favorites_package.py"),
                "--export", str(export), "--staging", str(staging),
                "--volume-bytes", str(512 * 1024**2),
                "--file-bytes", str(64 * 1024**2),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((staging / "SHA256SUMS").is_file())
            validation = subprocess.run([
                sys.executable, str(ROOT / "scripts/validate_live_favorites_package.py"),
                "--staging", str(staging), "--expect-volume", "0",
            ], capture_output=True, text=True)
            self.assertEqual(validation.returncode, 0, validation.stderr)
            summary = json.loads(validation.stdout)
            self.assertEqual(summary["files"], 3)
            manifest = json.loads((staging / "favorites_manifest.json").read_text())
            self.assertEqual({entry["folder"][0] for entry in manifest["entries"]},
                             {"Abertos", "Noticias"})
            self.assertTrue(all(len(entry["folder"]) == 1 for entry in manifest["entries"]))

    def test_package_refuses_pending_or_unavailable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = export_config()
            config["identities"][0]["status"] = "unavailable"
            config["identities"][0]["quality_status"] = None
            export = root / "export.json"
            export.write_text(json.dumps(config))
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_live_favorites_package.py"),
                "--export", str(export), "--staging", str(root / "staging"),
                "--volume-bytes", str(512 * 1024**2),
                "--file-bytes", str(32 * 1024**2),
            ], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("every identity approved", result.stderr)
            self.assertFalse((root / "staging").exists())

    def test_partial_staging_package_reports_pending_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = export_config()
            config["identities"][2]["status"] = "pending"
            config["identities"][2]["quality_status"] = "pending_no_native_1080"
            export = root / "export.json"
            export.write_text(json.dumps(config))
            staging = root / "staging"
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_live_favorites_package.py"),
                "--export", str(export), "--staging", str(staging), "--allow-partial",
                "--volume-bytes", str(512 * 1024**2),
                "--file-bytes", str(64 * 1024**2),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((staging / "package_summary.json").read_text())
            self.assertFalse(summary["complete_grade"])
            self.assertEqual(summary["identities"], 2)
            self.assertEqual(summary["pending"][0]["identity"], "cnn-brasil")
            manifest = json.loads((staging / "favorites_manifest.json").read_text())
            self.assertEqual(len(manifest["entries"]), 2)
            self.assertNotIn("CNN Brasil.trp",
                             [entry["filename"] for entry in manifest["entries"]])


if __name__ == "__main__":
    unittest.main()
