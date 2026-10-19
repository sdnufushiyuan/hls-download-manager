"""Offline checks: synthetic shapes/GeoTIFFs only; no Earth Engine network/auth."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import Affine
from shapely.geometry import Polygon, Point, box

import download_hls_optram as hls


class OfflineChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def create_shp(self, geometry=None):
        path = self.root / "roi.shp"
        gpd.GeoDataFrame(geometry=[geometry if geometry is not None else box(116, 30, 116.01, 30.01)],
                         crs="EPSG:4326").to_file(path)
        return path

    def create_tile(self, path, tile, values=None):
        if values is None:
            values = np.empty((6, tile.height, tile.width), dtype="float32")
            for index, value in enumerate((0.1, 0.3, 0.2, 0.5, 1.6, 64)):
                values[index] = value
        with rasterio.open(path, "w", driver="GTiff", width=tile.width, height=tile.height,
                           count=6, dtype="float32", crs="EPSG:32650", transform=Affine(*tile.transform)) as dest:
            dest.write(values)

    def test_qa_rejects_cloud_buffer_shadow_snow_water_and_nonlow_aerosol(self):
        qa = np.array([64, 65, 0, 128, 192] + [64 | (1 << bit) for bit in range(1, 6)])
        np.testing.assert_array_equal(hls.strict_clear_land(qa),
                                      [True, True, False, False, False] + [False] * 5)
        np.testing.assert_array_equal(hls.strict_clear_land([0, 64, 128, 192], "climatology-low"),
                                      [True, True, False, False])

    def test_local_shp_reprojects_and_keeps_original_files(self):
        path = self.create_shp()
        before = set(self.root.iterdir())
        geometry, crs, payload = hls.load_roi(path)
        self.assertTrue(crs.is_projected)
        self.assertGreater(geometry.area, 1e5)
        self.assertEqual(payload["features"][0]["properties"], {})
        self.assertEqual(before, set(self.root.iterdir()))
        path.with_suffix(".prj").unlink()
        with self.assertRaisesRegex(hls.DownloadError, "配套文件"):
            hls.load_roi(path)

    def test_points_require_explicit_metric_buffer_and_geographic_output_is_rejected(self):
        path = self.create_shp(Point(116, 30))
        with self.assertRaisesRegex(hls.DownloadError, "必须是面"):
            hls.load_roi(path)
        geometry, _, _ = hls.load_roi(path, buffer_m=100)
        self.assertAlmostEqual(geometry.area, np.pi * 100 ** 2, delta=150)
        with self.assertRaisesRegex(hls.DownloadError, "单位为米"):
            hls.load_roi(path, target_crs="EPSG:4326", buffer_m=100)

    def test_grid_snaps_30m_uses_tail_dimensions_and_skips_holes(self):
        roi = Polygon([(5, 5), (6005, 5), (6005, 6005), (5, 6005)],
                      holes=[[(1920, 2160), (3840, 2160), (3840, 4080), (1920, 4080)]])
        tiles = hls.make_tiles(roi, 64)
        self.assertTrue(any(t.width < 64 or t.height < 64 for t in tiles))
        self.assertTrue(all(t.left % 30 == 0 and t.top % 30 == 0 for t in tiles))
        self.assertFalse(any(t.left == 1920 and t.top == 4080 for t in tiles))
        with self.assertRaises(hls.DownloadError):
            hls.make_tiles(roi, 10000)

    def test_tile_validation_and_resume_detects_corruption(self):
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        path = self.root / tile.name
        self.create_tile(path, tile)
        stats = hls.inspect_tile(path, tile, "EPSG:32650", annotate=True,
                                 tags={"recipe_hash": "recipe", "scene_id": "scene"})
        self.assertEqual(stats["valid_pixels"], 12)
        with rasterio.open(path) as dataset:
            red, nir, swir, ndvi, str_values, qa = dataset.read()
            np.testing.assert_allclose(ndvi, (nir - red) / (nir + red), rtol=1e-6)
            np.testing.assert_allclose(str_values, (1 - swir) ** 2 / (2 * swir), rtol=1e-6)
        guard = hls.MountGuard(self.root)
        hls.write_json(path.with_suffix(".json"), {"recipe_hash": "recipe", "scene_id": "scene", "sha256": hls.file_hash(path)}, guard)
        self.assertIsNotNone(hls.cached_tile(path, tile, "EPSG:32650", "recipe", "low", "scene"))
        self.assertIsNone(hls.cached_tile(path, tile, "EPSG:32650", "recipe", "low", "other-scene"))
        with path.open("ab") as stream:
            stream.write(b"changed")
        self.assertIsNone(hls.cached_tile(path, tile, "EPSG:32650", "recipe", "low", "scene"))

    def test_scale_nonfinite_partial_mask_noninteger_qa_and_wrong_grid_are_rejected(self):
        tile = hls.Tile(0, 0, 300000, 3300000, 2, 2)
        values = np.zeros((6, 2, 2), dtype="float32")
        values[0:3] = 0.2
        values[3] = 0
        values[4] = 1.6
        values[5] = 64
        for band, bad, message in ((0, 2000, "反射率"), (4, np.inf, "NaN/Inf"),
                                  (0, hls.NODATA, "NoData 掩膜"), (5, 64.9, "整数")):
            with self.subTest(band=band, value=bad):
                changed = values.copy()
                changed[band] = bad
                path = self.root / "bad.tif"
                self.create_tile(path, tile, changed)
                with self.assertRaisesRegex(hls.DownloadError, message):
                    hls.inspect_tile(path, tile, "EPSG:32650", annotate=True)
        path = self.root / "wrong_grid.tif"
        self.create_tile(path, tile, values)
        with self.assertRaisesRegex(hls.DownloadError, "网格"):
            hls.inspect_tile(path, hls.Tile(0, 0, 300030, 3300000, 2, 2), "EPSG:32650", annotate=True)

    def test_empty_tile_is_valid_nodata(self):
        tile = hls.Tile(0, 0, 300000, 3300000, 2, 2)
        path = self.root / "empty.tif"
        self.create_tile(path, tile, np.full((6, 2, 2), hls.NODATA, dtype="float32"))
        self.assertEqual(hls.inspect_tile(path, tile, "EPSG:32650", annotate=True)["valid_pixels"], 0)

    def test_download_exact_grid_finalize_and_skip_verified_tile(self):
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        source = self.root / "source.tif"
        self.create_tile(source, tile)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.return_value = "https://example.invalid/signed?token=do-not-log"
        response = session.get.return_value.__enter__.return_value
        content = source.read_bytes()
        response.iter_content.return_value = [content[:50], content[50:]]
        output = self.root / "output" / tile.name
        guard = hls.MountGuard(output.parent)
        stats, skipped = hls.download_tile(image, tile, output, "EPSG:32650", session,
                                          guard, "recipe", "low", "scene")
        self.assertFalse(skipped)
        self.assertEqual(stats["valid_pixels"], 12)
        parameters = image.getDownloadURL.call_args.args[0]
        self.assertEqual(parameters, {"crs": "EPSG:32650", "crs_transform": tile.transform,
                                     "dimensions": [4, 3], "format": "GEO_TIFF"})
        self.assertFalse(output.with_name(output.name + ".part").exists())
        record = json.loads(output.with_suffix(".json").read_text())
        self.assertEqual(record["scene_id"], "scene")
        self.assertEqual(record["storage"]["encoding"], "DEFLATE")
        self.assertEqual(record["storage"]["stored_bytes"], output.stat().st_size)
        self.assertEqual(record["sha256"], hls.file_hash(output))
        with rasterio.open(output) as dataset:
            self.assertEqual(dataset.compression, rasterio.enums.Compression.deflate)
            self.assertEqual(dataset.tags(ns="IMAGE_STRUCTURE")["PREDICTOR"], "3")
        self.assertNotIn("token", json.dumps(record))
        _, skipped = hls.download_tile(image, tile, output, "EPSG:32650", session,
                                      guard, "recipe", "low", "scene")
        self.assertTrue(skipped)
        self.assertEqual(session.get.call_count, 1)

    def test_deflate_roundtrip_preserves_bits_metadata_and_valid_cache(self):
        tile = hls.Tile(0, 0, 300000, 3300000, 131, 117)
        row, col = np.indices((tile.height, tile.width), dtype="float32")
        values = np.empty((6, tile.height, tile.width), dtype="float32")
        values[0] = 0.1 + row * 0.0001
        values[1] = 0.3 + col * 0.0001
        values[2] = 0.2 + (row + col) * 0.0001
        values[3] = (values[1] - values[0]) / (values[1] + values[0])
        values[4] = (1 - values[2]) ** 2 / (2 * values[2])
        values[5] = 64 + (col % 2)
        # Include shared NoData, negative reflectance and signed zero exactly.
        values[:, 10:45, 20:85] = hls.NODATA
        values[0, 0, 0] = -0.0
        values[0, 0, 1] = -0.0001
        source = self.root / "uncompressed.tif"
        self.create_tile(source, tile, values)
        tags = {"recipe_hash": "recipe", "scene_id": "scene",
                "aerosol": "low", "resampling": "nearest"}
        hls.inspect_tile(source, tile, "EPSG:32650", annotate=True, tags=tags)
        with rasterio.open(source, "r+") as dataset:
            dataset.update_tags(test_value="source-metadata")
            dataset.update_tags(ns="research", roi="synthetic")
            dataset.update_tags(1, source_band="B4")
            dataset.update_tags(2, ns="research", sensor="offline")
            dataset.set_band_unit(1, "reflectance")
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.return_value = "https://example.invalid/signed?token=do-not-log"
        response = session.get.return_value.__enter__.return_value
        response.iter_content.return_value = [source.read_bytes()]
        output = self.root / "output" / tile.name
        stats, skipped = hls.download_tile(image, tile, output, "EPSG:32650", session,
                                          hls.MountGuard(output.parent), "recipe", "low", "scene")
        self.assertFalse(skipped)
        self.assertEqual(stats["valid_pixels"], tile.width * tile.height - 35 * 65)
        with rasterio.open(source) as before, rasterio.open(output) as after:
            np.testing.assert_array_equal(before.read().view(np.uint32), after.read().view(np.uint32))
            np.testing.assert_array_equal(before.read_masks(), after.read_masks())
            for field in ("crs", "transform", "nodatavals", "descriptions", "dtypes",
                          "scales", "offsets", "units", "colorinterp"):
                self.assertEqual(getattr(before, field), getattr(after, field), field)
            self.assertEqual(before.tags(), after.tags())
            self.assertEqual(before.tags(ns="research"), after.tags(ns="research"))
            self.assertEqual(before.tags(1), after.tags(1))
            self.assertEqual(before.tags(2, ns="research"), after.tags(2, ns="research"))
        self.assertLess(output.stat().st_size, source.stat().st_size // 2)
        self.assertIsNotNone(hls.cached_tile(output, tile, "EPSG:32650", "recipe", "low", "scene"))
        self.assertEqual({item.name for item in output.parent.iterdir()},
                         {output.name, output.with_suffix(".json").name})

    def test_deflate_rejects_changed_pixels_without_replacing_original(self):
        from rasterio.shutil import copy as real_copy
        tile = hls.Tile(0, 0, 300000, 3300000, 64, 64)
        path = self.root / "source.tif.part"
        self.create_tile(path, tile)
        hls.inspect_tile(path, tile, "EPSG:32650", annotate=True)
        original = hls.file_hash(path)
        def corrupt_copy(source, destination, **options):
            real_copy(source, destination, **options)
            with rasterio.open(destination, "r+") as dataset:
                changed = dataset.read(3)
                changed[0, 0] += 0.001
                dataset.write(changed, 3)
        with patch("rasterio.shutil.copy", side_effect=corrupt_copy):
            with self.assertRaisesRegex(hls.DownloadError, "像元值或掩膜"):
                hls.compress_tile_losslessly(path, tile, "EPSG:32650",
                                            hls.MountGuard(self.root), None, "low")
        self.assertEqual(hls.file_hash(path), original)
        self.assertEqual(list(self.root.iterdir()), [path])

    @unittest.skipUnless(os.name == "posix" and Path("/proc/self/fd").is_dir(),
                         "Pinned dir_fd compression requires Linux /proc")
    def test_deflate_mount_failure_uses_pinned_parent_and_cleans_own_partial(self):
        from rasterio.shutil import copy as real_copy
        tile = hls.Tile(0, 0, 300000, 3300000, 64, 64)
        path = self.root / "source.tif.part"
        self.create_tile(path, tile)
        hls.inspect_tile(path, tile, "EPSG:32650", annotate=True)
        original = hls.file_hash(path)
        guard = hls.MountGuard(self.root)
        copied = False
        def copy_then_lose_mount(source, destination, **options):
            nonlocal copied
            self.assertTrue(str(destination).startswith("/proc/self/fd/"))
            real_copy(source, destination, **options)
            copied = True
        def check_mount():
            if copied:
                raise hls.DownloadError("NAS 未挂载，禁止继续写入")
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            pinned = Path(f"/proc/self/fd/{descriptor}/{path.name}")
            with patch("rasterio.shutil.copy", side_effect=copy_then_lose_mount), \
                    patch.object(guard, "check", side_effect=check_mount):
                with self.assertRaisesRegex(hls.DownloadError, "NAS 未挂载"):
                    hls.compress_tile_losslessly(pinned, tile, "EPSG:32650", guard,
                                                descriptor, "low")
            self.assertEqual(hls.file_hash(pinned), original)
        finally:
            os.close(descriptor)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_failed_download_retries_without_leaking_signed_url(self):
        import requests
        tile = hls.Tile(0, 0, 300000, 3300000, 2, 2)
        image, session = MagicMock(), MagicMock()
        secret_url = "https://example.invalid/signed?token=do-not-log"
        image.getDownloadURL.return_value = secret_url
        session.get.side_effect = requests.exceptions.ConnectionError(secret_url)
        output = self.root / "output" / tile.name
        captured = io.StringIO()
        with patch.object(hls.time, "sleep"), contextlib.redirect_stdout(captured):
            with self.assertRaises(hls.TransientDownloadError) as failure:
                hls.download_tile(image, tile, output, "EPSG:32650", session,
                                  hls.MountGuard(output.parent), "recipe", "low", "scene")
        self.assertEqual(image.getDownloadURL.call_count, 6)
        self.assertNotIn("do-not-log", str(failure.exception) + captured.getvalue())
        self.assertFalse(output.exists())

    def test_transient_classifier_distinguishes_http_and_proxy_from_disk_errors(self):
        import requests
        import httplib2
        from googleapiclient.errors import HttpError
        for status, expected in ((429, True), (503, True), (400, False), (401, False), (403, False)):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                error = requests.exceptions.HTTPError('do-not-log', response=response)
                self.assertEqual(hls.is_transient_error(error), expected)
                wrapped = RuntimeError('EE wrapper')
                wrapped.__context__ = HttpError(httplib2.Response({'status': str(status)}), b'{}')
                self.assertEqual(hls.is_transient_error(wrapped), expected)
        self.assertTrue(hls.is_transient_error(httplib2.socks.GeneralProxyError('do-not-log')))
        self.assertTrue(hls.is_transient_error(requests.exceptions.ChunkedEncodingError('do-not-log')))
        self.assertFalse(hls.is_transient_error(OSError(28, 'NAS full')))
        storage_error = OSError(28, 'NAS full while closing interrupted stream')
        storage_error.__context__ = requests.exceptions.ChunkedEncodingError('preceding stream failure')
        self.assertFalse(hls.is_transient_error(storage_error))
        self.assertFalse(hls.is_transient_error(hls.DownloadError('QA failed')))

    def test_tls_eof_is_transient_but_certificates_auth_and_storage_remain_terminal(self):
        import ssl
        import requests
        from google.auth.exceptions import RefreshError
        eof = ssl.SSLEOFError(8, 'do-not-log')
        self.assertTrue(hls.is_transient_error(eof))
        wrapped = RuntimeError('transport wrapper')
        wrapped.__cause__ = eof
        self.assertTrue(hls.is_transient_error(wrapped))
        for error in (OSError(28, 'NAS full'), RefreshError('grant rejected'),
                      ssl.SSLCertVerificationError(1, 'certificate rejected')):
            error.__context__ = eof
            self.assertFalse(hls.is_transient_error(error))
        certificate = requests.exceptions.SSLError('do-not-log')
        certificate.__cause__ = ssl.SSLCertVerificationError(1, 'certificate rejected')
        self.assertFalse(hls.is_transient_error(certificate))

    def test_tls_eof_url_request_retries_with_new_url_and_preserves_committed_cache(self):
        import ssl
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        source = self.root / 'tls-source.tif'
        self.create_tile(source, tile)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.side_effect = [ssl.SSLEOFError(8, 'token=do-not-log'),
                                           'https://example.invalid/new']
        response = session.get.return_value.__enter__.return_value
        response.iter_content.return_value = [source.read_bytes()]
        output = self.root / 'output' / tile.name
        captured = io.StringIO()
        guard = hls.MountGuard(output.parent)
        with patch.object(hls.time, 'sleep') as pause, contextlib.redirect_stdout(captured):
            stats, reused = hls.download_tile(image, tile, output, 'EPSG:32650', session,
                                              guard, 'recipe', 'low', 'scene')
        pause.assert_called_once_with(5)
        self.assertEqual(image.getDownloadURL.call_count, 2)
        self.assertEqual(stats['valid_pixels'], 12)
        self.assertFalse(reused)
        self.assertNotIn('do-not-log', captured.getvalue())
        digest = hls.file_hash(output)
        image.getDownloadURL.reset_mock()
        _, reused = hls.download_tile(image, tile, output, 'EPSG:32650', session,
                                      guard, 'recipe', 'low', 'scene')
        self.assertTrue(reused)
        image.getDownloadURL.assert_not_called()
        self.assertEqual(hls.file_hash(output), digest)

    def test_tls_eof_exhaustion_is_bounded_and_returns_retryable_failure(self):
        import ssl
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.side_effect = ssl.SSLEOFError(8, 'token=do-not-log')
        output = self.root / 'output' / tile.name
        captured = io.StringIO()
        with patch.object(hls.time, 'sleep') as pause, contextlib.redirect_stdout(captured), \
                self.assertRaises(hls.TransientDownloadError) as failure:
            hls.download_tile(image, tile, output, 'EPSG:32650', session,
                              hls.MountGuard(output.parent), 'recipe', 'low', 'scene')
        self.assertEqual(image.getDownloadURL.call_count, 6)
        self.assertEqual([c.args[0] for c in pause.call_args_list], [5, 15, 30, 60, 120])
        session.get.assert_not_called()
        self.assertFalse(output.exists())
        self.assertNotIn('do-not-log', captured.getvalue() + str(failure.exception))

    def test_tls_eof_stream_partial_is_removed_before_retry(self):
        import ssl
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        source = self.root / 'tls-stream-source.tif'
        self.create_tile(source, tile)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.side_effect = ['https://example.invalid/first', 'https://example.invalid/second']
        response = session.get.return_value.__enter__.return_value
        data = source.read_bytes()
        def interrupted():
            yield data[:50]
            raise ssl.SSLEOFError(8, 'token=do-not-log')
        response.iter_content.side_effect = [interrupted(), iter([data])]
        output = self.root / 'output' / tile.name
        def check_cleanup(delay):
            self.assertEqual(delay, 5)
            self.assertEqual(list(output.parent.iterdir()), [])
        with patch.object(hls.time, 'sleep', side_effect=check_cleanup), contextlib.redirect_stdout(io.StringIO()):
            _, reused = hls.download_tile(image, tile, output, 'EPSG:32650', session,
                                          hls.MountGuard(output.parent), 'recipe', 'low', 'scene')
        self.assertFalse(reused)
        self.assertEqual(image.getDownloadURL.call_count, 2)
        self.assertIsNotNone(hls.cached_tile(output, tile, 'EPSG:32650', 'recipe', 'low', 'scene'))

    def test_google_auth_transport_wraps_http_protocol_error_but_auth_and_nas_stay_fatal(self):
        import http.client
        import httplib2
        import google_auth_httplib2
        from google.auth.exceptions import RefreshError, TransportError
        from googleapiclient.errors import HttpError
        transport = MagicMock()
        transport.request.side_effect = http.client.BadStatusLine('do-not-log')
        try:
            google_auth_httplib2.Request(transport)('https://example.invalid/refresh')
        except TransportError as error:
            self.assertTrue(hls.is_transient_error(error))
        else:
            self.fail('Expected Google-auth HTTP transport wrapper')
        self.assertTrue(hls.is_transient_error(TransportError('do-not-log')))
        rejected = RefreshError('invalid grant do-not-log')
        rejected.__context__ = TransportError('preceding request error')
        self.assertFalse(hls.is_transient_error(rejected))
        disk = OSError(28, 'NAS full')
        disk.__context__ = TransportError('preceding request error')
        self.assertFalse(hls.is_transient_error(disk))
        denied = TransportError('wrapped HTTP error')
        denied.__context__ = HttpError(httplib2.Response({'status': '403'}), b'{}')
        self.assertFalse(hls.is_transient_error(denied))

    def test_google_auth_transport_retries_then_commits_valid_tile_without_secret_output(self):
        from google.auth.exceptions import TransportError
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        source = self.root / 'source.tif'
        self.create_tile(source, tile)
        image, session = MagicMock(), MagicMock()
        secret = 'https://example.invalid/signed?token=do-not-log'
        image.getDownloadURL.side_effect = [TransportError(secret), secret]
        response = session.get.return_value.__enter__.return_value
        response.iter_content.return_value = [source.read_bytes()]
        output = self.root / 'output' / tile.name
        guard = hls.MountGuard(output.parent)
        captured = io.StringIO()
        with patch.object(hls.time, 'sleep') as pause, contextlib.redirect_stdout(captured):
            stats, skipped = hls.download_tile(image, tile, output, 'EPSG:32650', session,
                                              guard, 'recipe', 'low', 'scene')
        self.assertFalse(skipped)
        self.assertEqual(stats['valid_pixels'], 12)
        self.assertEqual(image.getDownloadURL.call_count, 2)
        pause.assert_called_once_with(5)
        self.assertNotIn('do-not-log', captured.getvalue())
        self.assertIsNotNone(hls.cached_tile(output, tile, 'EPSG:32650', 'recipe', 'low', 'scene'))

    def test_google_auth_transport_exhaustion_is_bounded_but_rejected_grant_never_retries(self):
        from google.auth.exceptions import RefreshError, TransportError
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        for error_type, expected_type, calls in ((TransportError, hls.TransientDownloadError, 6),
                                                (RefreshError, hls.DownloadError, 1)):
            with self.subTest(error_type=error_type):
                image, session = MagicMock(), MagicMock()
                image.getDownloadURL.side_effect = error_type('token=do-not-log')
                output = self.root / 'output' / tile.name
                captured = io.StringIO()
                with patch.object(hls.time, 'sleep') as pause, contextlib.redirect_stdout(captured), \
                        self.assertRaises(expected_type) as failure:
                    hls.download_tile(image, tile, output, 'EPSG:32650', session,
                                      hls.MountGuard(output.parent), 'recipe', 'low', 'scene')
                self.assertEqual(image.getDownloadURL.call_count, calls)
                self.assertEqual(pause.call_count, calls - 1)
                session.get.assert_not_called()
                self.assertFalse(output.exists())
                self.assertNotIn('do-not-log', captured.getvalue() + str(failure.exception))

    def test_opaque_ee_error_retries_once_and_commits_valid_tile(self):
        from ee.ee_exception import EEException
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        source = self.root / 'source.tif'
        self.create_tile(source, tile)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.side_effect = [EEException('token=do-not-log'),
                                            'https://example.invalid/signed?token=do-not-log']
        response = session.get.return_value.__enter__.return_value
        response.iter_content.return_value = [source.read_bytes()]
        output = self.root / 'output' / tile.name
        captured = io.StringIO()
        with patch.object(hls.time, 'sleep') as pause, contextlib.redirect_stdout(captured):
            stats, skipped = hls.download_tile(image, tile, output, 'EPSG:32650', session,
                                              hls.MountGuard(output.parent), 'recipe', 'low', 'scene')
        self.assertFalse(skipped)
        self.assertEqual(stats['valid_pixels'], 12)
        self.assertEqual(image.getDownloadURL.call_count, 2)
        pause.assert_called_once_with(5)
        self.assertNotIn('do-not-log', captured.getvalue())
        self.assertIsNotNone(hls.cached_tile(output, tile, 'EPSG:32650', 'recipe', 'low', 'scene'))

    def test_repeated_opaque_ee_error_enters_service_recovery_without_secret_output(self):
        from ee.ee_exception import EEException
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.side_effect = EEException('token=do-not-log')
        output = self.root / 'output' / tile.name
        captured = io.StringIO()
        with patch.object(hls.time, 'sleep') as pause, contextlib.redirect_stdout(captured), \
                self.assertRaises(hls.TransientDownloadError) as failure:
            hls.download_tile(image, tile, output, 'EPSG:32650', session,
                              hls.MountGuard(output.parent), 'recipe', 'low', 'scene')
        self.assertEqual(image.getDownloadURL.call_count, 2)
        pause.assert_called_once_with(5)
        session.get.assert_not_called()
        self.assertFalse(output.exists())
        self.assertNotIn('do-not-log', captured.getvalue() + str(failure.exception))
        with patch.object(hls, 'parser'), patch.object(hls, 'run', side_effect=failure.exception), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hls.main(), 75)

    def test_ee_wrapped_explicit_http_denial_never_retries(self):
        from ee.ee_exception import EEException
        from googleapiclient.errors import HttpError
        import httplib2
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        image, session = MagicMock(), MagicMock()
        denial = EEException('token=do-not-log')
        denial.__cause__ = HttpError(httplib2.Response({'status': '403'}), b'{}')
        image.getDownloadURL.side_effect = denial
        output = self.root / 'output' / tile.name
        with patch.object(hls.time, 'sleep') as pause, contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(hls.DownloadError):
            hls.download_tile(image, tile, output, 'EPSG:32650', session,
                              hls.MountGuard(output.parent), 'recipe', 'low', 'scene')
        self.assertEqual(image.getDownloadURL.call_count, 1)
        pause.assert_not_called()

    def test_interrupted_stream_is_removed_and_next_attempt_commits_valid_tile(self):
        import requests
        tile = hls.Tile(0, 0, 300000, 3300000, 4, 3)
        source = self.root / 'source.tif'
        self.create_tile(source, tile)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.side_effect = ['https://example.invalid/first', 'https://example.invalid/second']
        response = session.get.return_value.__enter__.return_value
        data = source.read_bytes()
        def interrupted():
            yield data[:50]
            raise requests.exceptions.ChunkedEncodingError('sensitive URL must not be logged')
        response.iter_content.side_effect = [interrupted(), iter([data])]
        output = self.root / 'output' / tile.name
        def check_cleanup(delay):
            self.assertEqual(delay, 5)
            self.assertEqual(list(output.parent.iterdir()), [])
        with patch.object(hls.time, 'sleep', side_effect=check_cleanup), contextlib.redirect_stdout(io.StringIO()):
            stats, skipped = hls.download_tile(image, tile, output, 'EPSG:32650', session,
                                              hls.MountGuard(output.parent), 'recipe', 'low', 'scene')
        self.assertFalse(skipped)
        self.assertEqual(stats['valid_pixels'], 12)
        self.assertEqual(image.getDownloadURL.call_count, 2)
        self.assertEqual({p.name for p in output.parent.iterdir()}, {tile.name, output.with_suffix('.json').name})
        self.assertIsNotNone(hls.cached_tile(output, tile, 'EPSG:32650', 'recipe', 'low', 'scene'))

    def test_nas_write_failure_is_not_retried_and_owned_partial_is_removed(self):
        tile = hls.Tile(0, 0, 300000, 3300000, 2, 2)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.return_value = 'https://example.invalid/signed?token=do-not-log'
        response = session.get.return_value.__enter__.return_value
        response.iter_content.return_value = [b'tile-bytes']
        output = self.root / 'output' / tile.name
        guard = hls.MountGuard(output.parent)
        original = guard.temporary_file
        def failing_stream(path, descriptor):
            stream, temporary = original(path, descriptor)
            wrapped = MagicMock()
            wrapped.__enter__.return_value = stream
            def fail_write(*args):
                raise OSError(28, 'NAS full')
            wrapped.write.side_effect = fail_write
            wrapped.__enter__.return_value = wrapped
            wrapped.__exit__.side_effect = lambda *args: stream.close()
            return wrapped, temporary
        with patch.object(guard, 'temporary_file', side_effect=failing_stream), \
                patch.object(hls.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(hls.DownloadError) as failure:
                hls.download_tile(image, tile, output, 'EPSG:32650', session, guard, 'recipe', 'low', 'scene')
        self.assertNotIsInstance(failure.exception, hls.TransientDownloadError)
        self.assertEqual(image.getDownloadURL.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(list(output.parent.iterdir()), [])

    def test_transport_exhaustion_main_uses_retryable_exit_code(self):
        with patch.object(hls, 'parser') as parser, \
                patch.object(hls, 'run', side_effect=hls.TransientDownloadError('safe network failure')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hls.main(), 75)

    def test_nas_close_error_during_interrupted_stream_is_terminal(self):
        import requests
        tile = hls.Tile(0, 0, 300000, 3300000, 2, 2)
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.return_value = 'https://example.invalid/signed?token=do-not-log'
        response = session.get.return_value.__enter__.return_value
        def interrupted():
            yield b'partial-tile'
            raise requests.exceptions.ChunkedEncodingError('preceding stream failure')
        response.iter_content.return_value = interrupted()
        output = self.root / 'output' / tile.name
        guard = hls.MountGuard(output.parent)
        original = guard.temporary_file
        def failing_close(path, descriptor):
            stream, temporary = original(path, descriptor)
            wrapped = MagicMock()
            wrapped.__enter__.return_value = wrapped
            wrapped.write.side_effect = stream.write
            def close_error(*args):
                stream.close()
                raise OSError(28, 'NAS close failure')
            wrapped.__exit__.side_effect = close_error
            return wrapped, temporary
        with patch.object(guard, 'temporary_file', side_effect=failing_close), \
                patch.object(hls.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(hls.DownloadError) as failure:
                hls.download_tile(image, tile, output, 'EPSG:32650', session, guard, 'recipe', 'low', 'scene')
        self.assertNotIsInstance(failure.exception, hls.TransientDownloadError)
        self.assertEqual(image.getDownloadURL.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(list(output.parent.iterdir()), [])
        with patch.object(hls, 'parser'), patch.object(hls, 'run', side_effect=failure.exception), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(hls.main(), 1)

    def test_dry_run_has_no_output_and_never_initializes_ee(self):
        path = self.create_shp()
        output = self.root / "output"
        args = hls.parser().parse_args(["--roi", str(path), "--out", str(output),
                                       "--start", "2024-01-01", "--end", "2024-02-01", "--dry-run"])
        with patch.object(hls, "initialize_ee", side_effect=AssertionError("No network allowed")):
            with contextlib.redirect_stdout(io.StringIO()):
                hls.run(args)
        self.assertFalse(output.exists())

    def test_proxy_setup_covers_token_refresh_and_restores_environment(self):
        import ee
        original = {"HTTPS_PROXY": "http://old:9", "NO_PROXY": "*"}
        observed = {}
        def fake_initialize(**kwargs):
            observed.update({name: os.environ.get(name) for name in ("HTTPS_PROXY", "NO_PROXY")})
            self.assertEqual(kwargs["project"], "dummy-project")
            self.assertIsNotNone(kwargs["http_transport"].proxy_info)
        with patch.dict(os.environ, original), patch.object(ee, "Initialize", side_effect=fake_initialize):
            hls.initialize_ee("dummy-project", "http://127.0.0.1:8118")
            self.assertEqual(os.environ["HTTPS_PROXY"], original["HTTPS_PROXY"])
            self.assertEqual(os.environ["NO_PROXY"], original["NO_PROXY"])
        self.assertEqual(observed["HTTPS_PROXY"], "http://127.0.0.1:8118")
        self.assertNotEqual(observed["NO_PROXY"], "*")
        for proxy in ("socks5://localhost:1080", "http://user:secret@localhost:8118"):
            with self.assertRaises(hls.DownloadError):
                hls.parse_proxy(proxy)

    def test_portable_project_config_is_read_beside_script_not_working_directory(self):
        script_dir = self.root / "copied_to_linux"
        script_dir.mkdir()
        config = script_dir / "hls_config.local.json"
        config.write_text(json.dumps({"project": "example-gee-project"}), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True), patch.object(hls, "__file__", str(script_dir / "download_hls_optram.py")):
            project, source = hls.resolve_project()
        self.assertEqual(project, "example-gee-project")
        self.assertEqual(source, "hls_config.local.json")

    def test_project_override_priority_and_bad_config_are_explicit(self):
        config = self.root / "hls_config.local.json"
        config.write_text(json.dumps({"project": "example-gee-project"}), encoding="utf-8")
        with patch.dict(os.environ, {"EE_PROJECT": "environment-project"}, clear=True):
            self.assertEqual(hls.resolve_project("explicit-project", config)[0], "explicit-project")
            self.assertEqual(hls.resolve_project(config_path=config)[0], "environment-project")
        config.write_text("not JSON", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(hls.DownloadError, "JSON 格式"):
                hls.resolve_project(config_path=config)

    def test_linux_mount_guard_stops_absent_mount_and_wrong_source(self):
        # Simulate /proc records; never touch the real NAS.
        guard = hls.MountGuard(self.root / "output", self.root, "//host/data")
        with patch.object(hls.sys, "platform", "linux"), patch.object(Path, "read_text", return_value=""):
            with self.assertRaisesRegex(hls.DownloadError, "未挂载"):
                guard.check()
        record = f"1 0 0:1 / {self.root} rw - cifs //different/data rw\n"
        with patch.object(hls.sys, "platform", "linux"), patch.object(Path, "read_text", return_value=record):
            with self.assertRaisesRegex(hls.DownloadError, "来源"):
                guard.check()

    def test_nas_free_space_floor_is_finite_nonnegative_and_local_outputs_ignore_it(self):
        with patch.dict(os.environ, {"HLS_MIN_FREE_GIB": "256"}):
            self.assertEqual(hls.MountGuard(self.root / "output", self.root).min_free_gib, 256)
            self.assertEqual(hls.MountGuard(self.root / "local_output").min_free_gib, 0)
        for value in ("-1", "nan", "inf", "not-a-number"):
            with self.subTest(value=value), patch.dict(os.environ, {"HLS_MIN_FREE_GIB": value}):
                with self.assertRaisesRegex(hls.DownloadError, "有限的非负"):
                    hls.MountGuard(self.root / "output", self.root)
                self.assertEqual(hls.MountGuard(self.root / "local_output").min_free_gib, 0)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hls.MountGuard(self.root / "output", self.root).min_free_gib, 0)

    @unittest.skipUnless(os.name == "posix" and Path("/proc/self/fd").is_dir(),
                         "Pinned NAS fstatvfs requires Linux")
    def test_low_nas_capacity_creates_nothing_and_releases_pinned_descriptor(self):
        with patch.dict(os.environ, {"HLS_MIN_FREE_GIB": "256"}):
            output = self.root / "new_download" / "scene" / "tile.tif"
            guard = hls.MountGuard(self.root / "new_download", self.root)
        # Use a real directory descriptor; simulate only capacity/mount identity.
        # The low-space decision must occur before mkdir or any file creation.
        space = MagicMock(f_bavail=16, f_frsize=4096)
        with patch.object(guard, "check", return_value="7"), \
                patch.object(Path, "read_text", return_value="mnt_id:\t7\n"), \
                patch.object(os, "fstatvfs", return_value=space) as capacity, \
                patch.object(os, "close", wraps=os.close) as close:
            with self.assertRaisesRegex(hls.DownloadError, "低于保留阈值 256 GiB"):
                with guard.writable_parent(output):
                    self.fail("A low-capacity NAS must never yield a writable parent")
            capacity.assert_called_once()
            descriptor = capacity.call_args.args[0]
            close.assert_called_once_with(descriptor)
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
