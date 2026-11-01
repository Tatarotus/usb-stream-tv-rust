"""Remote ADB must receive a real SSH-host path, never the local path.

The transfer goes local staging dir -> private VPS spool (`mktemp -d
/tmp/mixed-vod-XXXXXXXX`, mode 077) -> `adb push` on the SSH host. The only
destructive command the helper may ever form is `rm -rf -- <validated spool>`;
the tablet destination and the local directory are never deleted.

Documented residue limitation (behaviour asserted in
`test_ssh_death_during_cleanup_leaves_the_spool_on_the_vps`): if SSH dies while
the `finally` block runs, the remote `rm` never executes and the spool may
remain on the VPS until removed manually. The helper deliberately does not
escalate to any broader removal to compensate, and the cleanup exception
replaces the original transfer error. Both facts are reported as a limitation;
`scripts/mixed_ssh_transfer.py` is owned by another agent and is not edited
here.
"""
from pathlib import Path
from subprocess import CompletedProcess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from mixed_ssh_transfer import push_via_ssh

DEVICE_DIR = '/data/local/tmp/live-favorites/generation'
VALID_SPOOL = '/tmp/mixed-vod-Ab12Cd34'


class FakeTransport:
    ssh_host = 'oracle'

    def __init__(self, spool=VALID_SPOOL, *, host=None, fail_mktemp=False,
                 fail_copy=False, fail_adb=False, fail_cleanup=False,
                 cleanup_status=0):
        self.spool = spool
        self.fail_mktemp = fail_mktemp
        self.fail_copy = fail_copy
        self.fail_adb = fail_adb
        self.fail_cleanup = fail_cleanup
        self.cleanup_status = cleanup_status
        self.calls = []
        if host is not None:
            self.ssh_host = host

    @staticmethod
    def _kind(argv):
        last = argv[-1] if argv else ''
        if argv[0] == 'ssh':
            if 'mktemp' in last:
                return 'mktemp'
            if 'rm -rf' in last:
                return 'cleanup'
            return 'ssh-other'
        return argv[0]

    def _run(self, argv, *, check, timeout):
        self.calls.append(list(argv))
        kind = self._kind(argv)
        if kind == 'mktemp':
            if self.fail_mktemp:
                raise RuntimeError('mktemp failed')
            return CompletedProcess(argv, 0, self.spool, '')
        if kind == 'scp':
            if self.fail_copy:
                raise RuntimeError('copy failed')
            return CompletedProcess(argv, 0, '', '')
        if kind == 'cleanup':
            if self.fail_cleanup:
                raise RuntimeError('ssh died during cleanup')
            return CompletedProcess(
                argv, self.cleanup_status, '',
                'rm failed' if self.cleanup_status else '')
        raise AssertionError(f'unexpected _run command: {argv}')

    def adb(self, *args, check=True):
        self.calls.append(list(args))
        if self.fail_adb:
            raise RuntimeError('adb failed')
        return CompletedProcess(args, 0, '', '')


def destructive_targets(calls):
    """Full text of every remote command that could delete data."""
    commands = []
    for argv in calls:
        if argv[0] == 'ssh' and 'rm -rf' in ' '.join(argv):
            commands.append(argv[-1])
        elif argv[0] == 'rm':
            commands.append(' '.join(argv))
    return commands


class TransferCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.local = Path(self._temp.name)
        self.artifact = self.local / 'artifact.bin'
        self.artifact.write_bytes(b'payload')

    def tearDown(self):
        self._temp.cleanup()

    def assert_local_untouched(self):
        self.assertTrue(self.local.is_dir())
        self.assertEqual(self.artifact.read_bytes(), b'payload')

    def assert_only_validated_spool_removed(self, calls):
        """Exactly one destructive command, targeting only the validated spool."""
        self.assertEqual(destructive_targets(calls), ['rm -rf -- ' + VALID_SPOOL])
        for command in destructive_targets(calls):
            self.assertNotIn(DEVICE_DIR, command)
            self.assertNotIn(str(self.local), command)


class RemoteTransferTests(TransferCase):
    def test_copies_local_to_remote_before_adb_and_cleans_private_spool(self):
        t = FakeTransport()
        push_via_ssh(t, self.local, DEVICE_DIR)
        self.assertEqual([x[0] for x in t.calls], ['ssh', 'scp', 'push', 'ssh'])
        # The scp source is the real local directory, not a path on the VPS.
        self.assertEqual(t.calls[1][-2], str(self.local.resolve()) + '/.')
        # ADB on the SSH host receives the remote spool, never the local path.
        self.assertEqual(t.calls[2][2], VALID_SPOOL + '/.')
        self.assertEqual(t.calls[2][3], DEVICE_DIR + '/')
        self.assertNotIn(str(self.local), ' '.join(t.calls[2]))
        self.assertEqual(t.calls[3][-1], 'rm -rf -- ' + VALID_SPOOL)
        self.assert_local_untouched()
        self.assert_only_validated_spool_removed(t.calls)

    def test_failed_copy_cleans_only_validated_spool_and_never_pushes(self):
        t = FakeTransport(fail_copy=True)
        with self.assertRaises(RuntimeError):
            push_via_ssh(t, self.local, DEVICE_DIR)
        self.assertEqual([x[0] for x in t.calls], ['ssh', 'scp', 'ssh'])
        self.assert_local_untouched()
        self.assert_only_validated_spool_removed(t.calls)

    def test_unexpected_spool_is_not_used_or_deleted(self):
        for unsafe in ('/tmp/other',
                       '/tmp/mixed-vod-short',
                       '/tmp/mixed-vod-Ab12Cd34/../etc',
                       '/tmp/mixed-vod-Ab12Cd34; rm -rf /',
                       '/root'):
            with self.subTest(spool=unsafe):
                t = FakeTransport(spool=unsafe)
                with self.assertRaises(ValueError):
                    push_via_ssh(t, self.local, DEVICE_DIR)
                self.assertEqual(len(t.calls), 1)
                self.assertEqual(destructive_targets(t.calls), [])
                self.assert_local_untouched()

    def test_mktemp_failure_aborts_before_copy_or_removal(self):
        t = FakeTransport(fail_mktemp=True)
        with self.assertRaises(RuntimeError):
            push_via_ssh(t, self.local, DEVICE_DIR)
        self.assertEqual([x[0] for x in t.calls], ['ssh'])
        self.assertEqual(destructive_targets(t.calls), [])
        self.assert_local_untouched()

    def test_failed_adb_also_cleans_remote_spool(self):
        t = FakeTransport(fail_adb=True)
        with self.assertRaises(RuntimeError):
            push_via_ssh(t, self.local, DEVICE_DIR)
        self.assertEqual([x[0] for x in t.calls], ['ssh', 'scp', 'push', 'ssh'])
        self.assert_local_untouched()
        self.assert_only_validated_spool_removed(t.calls)

    def test_cleanup_nonzero_status_is_not_fatal_after_successful_push(self):
        # `rm` returning non-zero with check=False must not mask the transfer
        # result nor trigger a second, broader removal.
        t = FakeTransport(cleanup_status=1)
        result = push_via_ssh(t, self.local, DEVICE_DIR)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([x[0] for x in t.calls], ['ssh', 'scp', 'push', 'ssh'])
        self.assert_local_untouched()
        self.assert_only_validated_spool_removed(t.calls)

    def test_cleanup_transport_failure_propagates_without_unsafe_command(self):
        t = FakeTransport(fail_adb=True, fail_cleanup=True)
        with self.assertRaises(RuntimeError) as caught:
            push_via_ssh(t, self.local, DEVICE_DIR)
        self.assertEqual(str(caught.exception), 'ssh died during cleanup')
        self.assertEqual([x[0] for x in t.calls], ['ssh', 'scp', 'push', 'ssh'])
        self.assert_local_untouched()
        self.assert_only_validated_spool_removed(t.calls)

    def test_ssh_death_during_cleanup_leaves_the_spool_on_the_vps(self):
        """Documented residue: a dead SSH session cannot run the remote `rm`.

        The spool `/tmp/mixed-vod-XXXXXXXX` (mode 077) may therefore remain on
        the VPS until removed manually. The helper must not compensate with any
        broader deletion, and because the `finally` exception wins, the original
        transfer error is replaced by the cleanup error (reported limitation).
        """
        t = FakeTransport(fail_adb=True, fail_cleanup=True)
        with self.assertRaises(RuntimeError) as caught:
            push_via_ssh(t, self.local, DEVICE_DIR)
        self.assertNotEqual(str(caught.exception), 'adb failed')
        # The final attempt is the exact, validated cleanup; it failed, so the
        # least-privilege spool is the residue.
        self.assertEqual(t.calls[-1][-1], 'rm -rf -- ' + VALID_SPOOL)
        self.assertEqual(destructive_targets(t.calls), ['rm -rf -- ' + VALID_SPOOL])
        self.assert_local_untouched()

    def test_invalid_host_and_device_are_rejected_before_any_call(self):
        for host in ('', 'oracle;rm -rf /', 'oracle host', '-oProxyCommand=x'):
            with self.subTest(host=host):
                t = FakeTransport(host=host)
                with self.assertRaises(ValueError):
                    push_via_ssh(t, self.local, DEVICE_DIR)
                self.assertEqual(t.calls, [])
        t = FakeTransport()
        with self.assertRaises(ValueError):
            push_via_ssh(t, self.local, '/sdcard/movies')
        self.assertEqual(t.calls, [])
        self.assert_local_untouched()

    def test_missing_local_directory_is_rejected_before_any_call(self):
        t = FakeTransport()
        missing = self.local / 'absent'
        with self.assertRaises(OSError):
            push_via_ssh(t, missing, DEVICE_DIR)
        self.assertEqual(t.calls, [])
        self.assert_local_untouched()


if __name__ == '__main__':
    unittest.main()