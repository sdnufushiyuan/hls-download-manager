"""Provider error semantics and preservation of recoverable storage failures."""
import contextlib
import errno
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httplib2
from ee.ee_exception import EEException
from googleapiclient.errors import HttpError
import download_hls_optram as h


def wrapped(status, message, api_status='INVALID_ARGUMENT'):
    inner = HttpError(httplib2.Response({'status': str(status), 'reason': 'server'}),
                      json.dumps({'error': {'message': message, 'status': api_status}}).encode(),
                      uri='https://example.invalid/?token=private-test-token')
    outer = EEException('outer private-test-token')
    outer.__cause__ = inner
    return outer


class EERecoveryTests(unittest.TestCase):
    def test_known_ee_compute_400_errors_are_recoverable_with_safe_categories(self):
        for message, category in [('Internal error. private-test-token', 'ee-internal'),
                                  ('Computation timed out.', 'ee-compute-timeout'),
                                  ('Too many concurrent aggregations.', 'ee-concurrency-limit')]:
            with self.subTest(message=message):
                error = wrapped(400, message)
                self.assertTrue(h.is_transient_error(error))
                self.assertFalse(h.is_opaque_ee_error(error))
                self.assertEqual(h.error_diagnostic(error), f'http_status=400 category={category}')

    def test_authorization_bad_requests_missing_assets_and_memory_stay_terminal(self):
        for status, message in [(400, 'Image.select: Band pattern did not match'),
                                (400, 'Image.select: Invalid band Internal error'),
                                (400, 'User memory limit exceeded.'),
                                (401, 'Internal error.'), (403, 'Computation timed out.'),
                                (404, 'Image asset not found.')]:
            with self.subTest(status=status, message=message):
                error = wrapped(status, message)
                self.assertFalse(h.is_transient_error(error))
                self.assertFalse(h.is_opaque_ee_error(error))
                self.assertNotIn('private-test-token', h.error_diagnostic(error))

    def test_resource_limit_is_separate_from_memory_limit(self):
        self.assertTrue(h.is_transient_error(wrapped(400, 'Quota exceeded', 'RESOURCE_EXHAUSTED')))
        self.assertFalse(h.is_transient_error(wrapped(400, 'User memory limit exceeded', 'RESOURCE_EXHAUSTED')))

    def test_malformed_or_oversized_bodies_are_not_retried_as_compute_errors(self):
        for body in [b'not json', b'[]', b'{"error": []}', b'{"error":{"message":[]}}', b'x'*32769]:
            error = HttpError(httplib2.Response({'status': '400'}), b'{"error":{"message":"fixture"}}')
            error.content = body  # construct normally, then exercise malformed buffered payloads
            self.assertEqual(h.ee_http_category(error), 'invalid-request')
            self.assertFalse(h.is_transient_error(error))

    def test_local_or_credential_failure_is_not_hidden_by_network_context(self):
        from google.auth.exceptions import RefreshError
        remote = wrapped(400, 'Internal error.')
        local = PermissionError(errno.EACCES, 'private-test-token')
        local.__context__ = remote
        self.assertFalse(h.is_transient_error(local))
        self.assertIn('category=filesystem errno=13', h.error_diagnostic(local))
        auth = RefreshError('private-test-token')
        auth.__context__ = remote
        self.assertFalse(h.is_transient_error(auth))
        self.assertEqual(h.error_diagnostic(auth), 'http_status=none category=credentials')

    def test_tls_errno_is_not_reported_as_a_filesystem_failure(self):
        import ssl
        error = ssl.SSLEOFError(8, 'private-test-token')
        self.assertTrue(h.is_transient_error(error))
        self.assertEqual(h.error_diagnostic(error), 'http_status=none category=tls')

    def test_parallel_pool_preserves_storage_exit_type_instead_of_terminal_one(self):
        for workers in (1, 4, 8):
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as folder:
                tile = h.Tile(0, 0, 300000, 3300000, 4, 3)
                with patch.object(h, 'download_tile', side_effect=h.TransientStorageError('op=replace errno=13')), \
                        h.TileDownloadPool(workers, None) as pool, self.assertRaises(h.TransientStorageError):
                    pool.download_scene(MagicMock(), [tile], Path(folder), 'EPSG:32650',
                        h.MountGuard(folder), 'recipe', 'low',
                        {'sensor': 'S30', 'index': 'fixture', 'id': 'fixture'}, lambda _: None)

    def test_known_internal_error_retries_current_tile_and_commits_exact_values(self):
        import numpy as np
        import rasterio
        from rasterio.transform import Affine
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);tile = h.Tile(0, 0, 300000, 3300000, 4, 3)
            source = root/'source.tif'
            values = np.empty((6, 3, 4), dtype='float32')
            for n, value in enumerate((.1, .3, .2, .5, 1.6, 64)):values[n] = value
            with rasterio.open(source, 'w', driver='GTiff', width=4, height=3, count=6,
                               dtype='float32', crs='EPSG:32650', transform=Affine(*tile.transform)) as ds:
                ds.write(values)
            image, session = MagicMock(), MagicMock()
            image.getDownloadURL.side_effect = [wrapped(400, 'Internal error. private-test-token'),
                                                 'https://example.invalid/fixture']
            session.get.return_value.__enter__.return_value.iter_content.return_value = [source.read_bytes()]
            output = io.StringIO();target = root/'out'/tile.name
            with patch.object(h.time, 'sleep') as pause, contextlib.redirect_stdout(output):
                stats, reused = h.download_tile(image, tile, target, 'EPSG:32650', session,
                    h.MountGuard(target.parent), 'recipe', 'low', 'fixture')
            self.assertFalse(reused);self.assertEqual(stats['valid_pixels'], 12)
            pause.assert_called_once_with(5)
            with rasterio.open(target) as ds:np.testing.assert_array_equal(ds.read(), values)
            self.assertIn('http_status=400 category=ee-internal', output.getvalue())
            self.assertNotIn('private-test-token', output.getvalue())

    def test_compute_retry_exhaustion_is_bounded_and_requests_future_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            tile = h.Tile(0, 0, 300000, 3300000, 4, 3)
            image, session = MagicMock(), MagicMock()
            image.getDownloadURL.side_effect = wrapped(400, 'Internal error. private-test-token')
            capture = io.StringIO()
            with patch.object(h.time, 'sleep') as pause, contextlib.redirect_stdout(capture), \
                    self.assertRaises(h.TransientDownloadError) as error:
                h.download_tile(image, tile, Path(folder)/tile.name, 'EPSG:32650', session,
                                h.MountGuard(folder), 'recipe', 'low', 'fixture')
            self.assertEqual(image.getDownloadURL.call_count, 6)
            self.assertEqual(pause.call_count, 5)
            self.assertIn('http_status=400 category=ee-internal', str(error.exception))
            self.assertNotIn('private-test-token', capture.getvalue()+str(error.exception))
            session.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
