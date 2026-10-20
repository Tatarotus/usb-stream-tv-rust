"""TASK-010: importing server must not start workers, processes or network.

The subprocess is deliberate: it proves the import boundary in a clean
interpreter, not merely after another test already imported server.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestServerImportIsolation(unittest.TestCase):
    def test_import_has_no_process_thread_or_network_side_effects(self):
        script = textwrap.dedent(
            """
            import socket
            import subprocess
            import threading
            import urllib.request

            calls = []

            def fail_popen(*args, **kwargs):
                calls.append("subprocess.Popen")
                raise AssertionError("Popen called during import")

            def fail_thread_start(self):
                calls.append("threading.Thread.start")
                raise AssertionError("thread started during import")

            def fail_connect(self, *args, **kwargs):
                calls.append("socket.socket.connect")
                raise AssertionError("network connect during import")

            def fail_urlopen(*args, **kwargs):
                calls.append("urllib.request.urlopen")
                raise AssertionError("network urlopen during import")

            subprocess.Popen = fail_popen
            threading.Thread.start = fail_thread_start
            socket.socket.connect = fail_connect
            urllib.request.urlopen = fail_urlopen

            import server

            assert server.HUB._started is False, "module HUB was started"
            assert server.HUB.reader_thread is None, "reader thread created"
            assert server.HUB.keepalive_thread is None, "keepalive thread created"
            assert getattr(server.HUB, "proc", None) is None, "producer created"
            assert calls == [], "import side effects: %r" % (calls,)
            print("IMPORT_ISOLATION_OK")
            """
        )
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "import isolation subprocess failed\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.stdout, proc.stderr),
        )
        self.assertIn("IMPORT_ISOLATION_OK", proc.stdout)

    def test_import_with_empty_catalog_defers_sync_without_side_effects(self):
        """RC-001 ITERATION 2 regression: a clean checkout with an empty
        catalog cache must not start a thread or touch the network at import.
        The initial sync is deferred and must run exactly once, on an explicit
        runtime start."""
        empty_cache = tempfile.mkdtemp(prefix="rc001-empty-catalog-")
        script = textwrap.dedent(
            """
            import threading
            import urllib.request

            side_effects = []

            def fail_thread_start(self):
                side_effects.append("thread")
                raise AssertionError("thread started during import")

            def fail_urlopen(*args, **kwargs):
                side_effects.append("urlopen")
                raise AssertionError("network during import")

            threading.Thread.start = fail_thread_start
            urllib.request.urlopen = fail_urlopen

            import server

            assert server.CATALOG_MGR._needs_initial_sync is True, (
                "empty catalog did not schedule a deferred sync"
            )
            assert side_effects == [], "import side effects: %r" % (side_effects,)

            started = []
            server.CATALOG_MGR.sync_all_async = lambda: started.append("sync")
            server.CATALOG_MGR.start_background_sync()
            server.CATALOG_MGR.start_background_sync()
            assert started == ["sync"], (
                "deferred sync must run exactly once: %r" % (started,)
            )
            assert server.CATALOG_MGR._needs_initial_sync is False
            print("EMPTY_CATALOG_DEFERRAL_OK")
            """
        )
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["CATALOG_CACHE_DIR"] = empty_cache
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "empty-catalog import regression failed\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.stdout, proc.stderr),
        )
        self.assertIn("EMPTY_CATALOG_DEFERRAL_OK", proc.stdout)

    def test_run_starts_hub_before_serving(self):
        import server

        with mock.patch.object(server.HUB, "start") as start, mock.patch(
            "server.ThreadedHTTPServer", side_effect=RuntimeError("stop")
        ):
            with self.assertRaises(RuntimeError):
                server.run()
        start.assert_called_once_with()

    def test_default_streamhub_constructor_still_starts(self):
        import server

        with mock.patch.object(server.StreamHub, "start") as start:
            server.StreamHub()
        start.assert_called_once_with()

    def test_streamhub_start_is_explicit_and_idempotent(self):
        import server

        hub = server.StreamHub(start_workers=False)
        self.assertFalse(hub._started)
        self.assertIsNone(hub.reader_thread)
        self.assertIsNone(hub.keepalive_thread)

        with mock.patch.object(hub, "_start_initial") as initial, mock.patch(
            "server.threading.Thread"
        ) as thread_cls:
            hub.start()
            hub.start()
            initial.assert_called_once_with()
            self.assertEqual(thread_cls.call_count, 2)
            self.assertEqual(thread_cls.return_value.start.call_count, 2)


if __name__ == "__main__":
    unittest.main()
