"""Structural validation of the chosen mixed package with the Rust verifiers.

`sha256sum` alone does not prove the NTFS structure. `scripts/validate_mixed_package.sh`
expands the SPARSE01 template into a real sparse image and runs the host
`fuse_ntfs` startup path (layout/package/manifest/template/allowlist) against
it, so a green run here means the package passed the same verifiers the tablet
startup uses.

The tests skip only when the chosen package directory does not exist at all or
when the host toolchain needed to run the script is unavailable; a present but
broken package must fail.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_mixed_package.sh"
CHOSEN_PACKAGE = ROOT / "scratch" / "mixed-staging" / "mixed-gen2-32abe7d7"

HOST_TOOLS = ("bash", "sha256sum", "gzip", "python3", "stat", "find", "cargo")
HOST_BINARIES = (
    ROOT / "target" / "release" / "fuse_ntfs",
    ROOT / "target" / "release" / "sparse_unpack",
)
# The dummy validation token the script writes; it must never be printed.
DUMMY_TOKEN = "0" * 48


def toolchain_gap() -> list[str]:
    """Returns what is missing, or an empty list when the script can run.

    `cargo` is required because the script rebuilds the host `fuse_ntfs` /
    `sparse_unpack` whenever they are missing or older than their sources; a
    stale binary would run stale startup verifiers.
    """
    return [tool for tool in HOST_TOOLS if shutil.which(tool) is None]


def require_chosen_package() -> None:
    if not CHOSEN_PACKAGE.is_dir():
        raise unittest.SkipTest(
            f"chosen package directory does not exist: {CHOSEN_PACKAGE}"
        )


def run_script(package_dir: Path, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(package_dir)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ROOT,
    )


class ChosenPackageValidationTests(unittest.TestCase):
    def test_chosen_package_passes_startup_verifiers(self):
        require_chosen_package()
        missing = toolchain_gap()
        if missing:
            raise unittest.SkipTest(f"host toolchain unavailable: {', '.join(missing)}")

        result = run_script(CHOSEN_PACKAGE)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        for binary in HOST_BINARIES:
            self.assertTrue(binary.is_file(), binary)
        # Hashes, allowlist and the two host startup runs must all be reported.
        # The terminal state of each run depends on host FUSE privileges, so
        # only the host-independent parts are asserted here.
        self.assertIn("SHA256SUMS: 8 hashes conferem", result.stdout)
        self.assertIn("allowlist: 120 tasks", result.stdout)
        self.assertIn("imagem NTFS expandida:", result.stdout)
        self.assertTrue(
            any(line.startswith("[ok] run1: ") for line in result.stdout.splitlines()),
            result.stdout,
        )
        self.assertTrue(
            any(
                line.startswith("[ok] run2: allowlist aceita")
                for line in result.stdout.splitlines()
            ),
            result.stdout,
        )
        self.assertIn("PACOTE VALIDADO", result.stdout)
        self.assertIn("122 arquivos", result.stdout)
        # The dummy token is a local validation value and must never leak.
        self.assertNotIn(DUMMY_TOKEN, output)
        self.assertNotIn("Authorization", output)

    def test_missing_artifact_fails_closed(self):
        require_chosen_package()
        with tempfile.TemporaryDirectory(prefix="mixed-package-missing-") as temp:
            copy = Path(temp) / "package"
            shutil.copytree(CHOSEN_PACKAGE, copy)
            (copy / "vod_allowlist.json").unlink()

            result = run_script(copy, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("vod_allowlist.json", result.stderr)
            self.assertIn("obrigatório ausente", result.stderr)

    def test_tampered_artifact_is_rejected_by_hashes(self):
        require_chosen_package()
        with tempfile.TemporaryDirectory(prefix="mixed-package-tampered-") as temp:
            copy = Path(temp) / "package"
            shutil.copytree(CHOSEN_PACKAGE, copy)
            with (copy / "favorites_layout.json").open("ab") as handle:
                handle.write(b" ")

            result = run_script(copy, timeout=120)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA256SUMS não confere", result.stderr)


if __name__ == "__main__":
    sys.exit(unittest.main())