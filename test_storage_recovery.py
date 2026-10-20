"""NAS commit races: preserve bytes, bounded recovery, and terminal safeguards."""
import contextlib
import errno
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import download_hls_optram as h


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'summary.json'
        self.partial = self.root / 'summary.json.part'
        self.path.write_bytes(b'old-valid')
        self.partial.write_bytes(b'new-valid')
        self.guard = h.MountGuard(self.root)
        self.guard.mount = self.root  # simulate the mount boundary; real NAS tested separately
        self.guard.check = Mock(return_value='verified-mount')

    def test_share_denial_retries_same_bytes_without_unlink_or_rewrite(self):
        replace = os.replace
        calls = []
        def commit(*args, **kwargs):
            calls.append(args)
            self.assertEqual(self.path.read_bytes(), b'old-valid')
            self.assertEqual(self.partial.read_bytes(), b'new-valid')
            if len(calls) < 3:
                raise PermissionError(errno.EACCES, 'secret detail must not be logged')
            replace(*args, **kwargs)
        output = io.StringIO()
        with patch.object(h.os, 'replace', side_effect=commit), patch.object(h.time, 'sleep') as pause, \
                patch.object(h.os, 'unlink', side_effect=AssertionError('must not delete target')), \
                contextlib.redirect_stdout(output):
            self.guard.finalize(self.path, self.partial, None)
        self.assertEqual(self.path.read_bytes(), b'new-valid')
        self.assertFalse(self.partial.exists())
        self.assertEqual([c.args[0] for c in pause.call_args_list], [0.25, 1])
        self.assertEqual(self.guard.check.call_count, 3)
        self.assertIn('errno=13 file=summary.json', output.getvalue())
        self.assertNotIn('secret', output.getvalue())

    def test_exhaustion_preserves_target_and_json_partial_and_requests_resume(self):
        with patch.object(h.os, 'replace', side_effect=PermissionError(errno.EACCES, 'secret')), \
                patch.object(h.time, 'sleep') as pause, contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(h.TransientStorageError) as failure:
            self.guard.finalize(self.path, self.partial, None)
        self.assertEqual(pause.call_count, 4)
        self.assertEqual(self.path.read_bytes(), b'old-valid')
        self.assertEqual(self.partial.read_bytes(), b'new-valid')
        self.assertNotIn('secret', str(failure.exception))

    def test_unmounted_local_permission_and_permanent_errors_never_retry(self):
        for mount, number in ((None, errno.EACCES), (self.root, errno.ENOSPC),
                              (self.root, errno.EROFS), (self.root, errno.ENOENT)):
            with self.subTest(mount=mount, number=number):
                self.guard.mount = mount
                with patch.object(h.os, 'replace', side_effect=OSError(number, 'hidden')), \
                        patch.object(h.time, 'sleep') as pause, self.assertRaises(OSError):
                    self.guard.finalize(self.path, self.partial, None)
                pause.assert_not_called()

    def test_mount_change_during_retry_stops_before_another_commit(self):
        self.guard.check.side_effect = ['verified', h.DownloadError('mount changed')]
        with patch.object(h.os, 'replace', side_effect=PermissionError(errno.EACCES, 'hidden')) as commit, \
                patch.object(h.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(h.DownloadError, 'mount changed'):
            self.guard.finalize(self.path, self.partial, None)
        commit.assert_called_once()

    @unittest.skipUnless(os.name == 'posix', 'Linux dir-fd commit')
    def test_retry_keeps_pinned_parent_descriptor(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        replace = os.replace
        calls = []
        def commit(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                raise OSError(errno.EBUSY, 'busy')
            return replace(*args, **kwargs)
        with patch.object(h.os, 'replace', side_effect=commit), patch.object(h.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.guard.finalize(self.path, self.partial, fd)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0], (('summary.json.part', 'summary.json'),
                                   {'src_dir_fd': fd, 'dst_dir_fd': fd}))
        self.assertEqual(self.path.read_bytes(), b'new-valid')

    def test_cli_storage_failure_is_distinct_from_permanent_local_error(self):
        for error, expected in ((h.TransientStorageError('op=replace errno=13'), 76),
                                (PermissionError(errno.EACCES, 'secret'), 1)):
            with patch.object(h, 'parser') as parser, patch.object(h, 'run', side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                parser.return_value.parse_args.return_value = object()
                self.assertEqual(h.main(), expected)
                self.assertNotIn('secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
