import io
import json
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from game_manager.artwork import ArtworkError, SteamGridDB


def api_response(data, status=200):
    response = Mock()
    response.status_code = status
    response.json.return_value = {"success": True, "data": data}
    return response


def image_bytes(size=(600, 900), image_format="PNG"):
    content = io.BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(content, format=image_format)
    return content.getvalue()


def image_response(content, declared_size=None):
    response = Mock()
    response.status_code = 200
    response.headers = {} if declared_size is None else {"Content-Length": str(declared_size)}
    response.iter_content.return_value = [content]
    return response


def candidate(asset_id=123, name="作者"):
    return {"id": asset_id, "url": f"https://cdn2.steamgriddb.com/grid/{asset_id}.png", "author": {"name": name, "steam64": "100"}}


class SteamGridDBTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "artwork"
        self.client = SteamGridDB("private-test-key")

    def tearDown(self):
        self.temporary.cleanup()

    def make_previous(self):
        self.destination.mkdir()
        (self.destination / "cover.png").write_bytes(b"previous cover")
        (self.destination / "assets.json").write_text('{"previous": true}', encoding="utf-8")

    def assert_previous_unchanged(self):
        self.assertEqual((self.destination / "cover.png").read_bytes(), b"previous cover")
        self.assertEqual((self.destination / "assets.json").read_text(encoding="utf-8"), '{"previous": true}')
        self.assertEqual(set(self.root.iterdir()), {self.destination})

    @patch("game_manager.artwork.requests.get")
    def test_search_encodes_name_and_returns_game_fields(self, get):
        get.return_value = api_response([{"id": 42, "name": "Game + / Test", "release_date": 100}])
        self.assertEqual(self.client.search("  Game + / Test  "), [{"id": 42, "name": "Game + / Test", "release_date": 100}])
        self.assertEqual(get.call_args.args[0], self.client.BASE_URL + "/search/autocomplete/Game%20%2B%20%2F%20Test")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer private-test-key")
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 30))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        get.return_value.close.assert_called_once()

    @patch("game_manager.artwork.requests.get")
    def test_downloads_valid_assets_and_persists_manifest_without_key(self, get):
        sizes = [(600, 900), (920, 430), (1920, 620), (300, 120)]
        responses = []
        for index, size in enumerate(sizes):
            responses.extend([api_response([candidate(index + 1)]), image_response(image_bytes(size))])
        get.side_effect = responses
        self.make_previous()
        manifest = self.client.download_assets(42, self.destination)
        self.assertEqual(manifest["missing"], [])
        self.assertEqual(set(manifest["assets"]), {"cover", "wide", "hero", "logo"})
        persisted = (self.destination / "assets.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(persisted), manifest)
        self.assertNotIn("private-test-key", persisted)
        api_calls = get.call_args_list[::2]
        download_calls = get.call_args_list[1::2]
        self.assertEqual([call.kwargs["params"].get("dimensions") for call in api_calls], ["600x900", "920x430", "1920x620", None])
        self.assertEqual(api_calls[-1].kwargs["params"]["mimes"], "image/png")
        for call in api_calls:
            self.assertEqual(call.kwargs["params"]["types"], "static")
            self.assertEqual(call.kwargs["params"]["limit"], 5)
        for call in download_calls:
            self.assertNotIn("Authorization", call.kwargs["headers"])
            self.assertNotIn("private-test-key", call.args[0])
            self.assertTrue(call.kwargs["stream"])
            self.assertFalse(call.kwargs["allow_redirects"])
        for kind, size in zip(("cover", "wide", "hero", "logo"), sizes):
            record = manifest["assets"][kind]
            self.assertEqual((record["width"], record["height"]), size)
            self.assertEqual(record["author"]["name"], "作者")
            self.assertTrue((self.destination / record["file"]).is_file())
        self.assertEqual(set(self.root.iterdir()), {self.destination})

    @patch("game_manager.artwork.requests.get")
    def test_empty_results_are_recorded_as_missing(self, get):
        get.side_effect = [api_response([]) for _ in range(4)]
        manifest = self.client.download_assets(42, self.destination)
        self.assertEqual(manifest["assets"], {})
        self.assertEqual(manifest["missing"], ["cover", "wide", "hero", "logo"])
        self.assertEqual(get.call_count, 4)

    @patch("game_manager.artwork.requests.get")
    def test_chooses_first_candidate_with_required_metadata_dimensions(self, get):
        incorrect = candidate(1)
        incorrect.update({"width": 460, "height": 215})
        correct = candidate(2)
        correct.update({"width": 600, "height": 900})
        get.side_effect = [api_response([incorrect, correct]), image_response(image_bytes()), api_response([]), api_response([]), api_response([])]
        manifest = self.client.download_assets(42, self.destination)
        self.assertEqual(manifest["assets"]["cover"]["id"], 2)
        self.assertEqual(get.call_args_list[1].args[0], correct["url"])

    @patch("game_manager.artwork.requests.get")
    def test_missing_key_does_not_make_request(self, get):
        with self.assertRaisesRegex(ArtworkError, "API Key"):
            SteamGridDB("").search("Game")
        get.assert_not_called()

    @patch("game_manager.artwork.requests.get")
    def test_authorization_rate_limit_and_timeout_have_safe_errors(self, get):
        for status, expected in [(401, "缺失或无效"), (429, "过于频繁"), (404, "未找到")]:
            with self.subTest(status=status):
                get.return_value = api_response([], status)
                with self.assertRaisesRegex(ArtworkError, expected) as raised:
                    self.client.search("Game")
                self.assertNotIn("private-test-key", str(raised.exception))
        get.side_effect = requests.Timeout("private-test-key")
        with self.assertRaisesRegex(ArtworkError, "超时") as raised:
            self.client.search("Game")
        self.assertNotIn("private-test-key", str(raised.exception))
        self.assertNotIn("private-test-key", "".join(traceback.format_exception(type(raised.exception), raised.exception, raised.exception.__traceback__)))

    @patch("game_manager.artwork.requests.get")
    def test_failed_later_request_preserves_old_images(self, get):
        self.make_previous()
        get.side_effect = [api_response([candidate()]), image_response(image_bytes()), requests.ConnectionError("private-test-key")]
        with self.assertRaisesRegex(ArtworkError, "无法连接"):
            self.client.download_assets(42, self.destination)
        self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.get")
    def test_wrong_dimensions_and_damaged_images_preserve_previous_data(self, get):
        self.make_previous()
        for content, expected in [(image_bytes((600, 899)), "尺寸不正确"), (b"not a valid image", "已损坏")]:
            with self.subTest(expected=expected):
                get.side_effect = [api_response([candidate()]), image_response(content)]
                with self.assertRaisesRegex(ArtworkError, expected):
                    self.client.download_assets(42, self.destination)
                self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.get")
    def test_size_limit_applies_to_header_and_stream(self, get):
        self.make_previous()
        self.client.MAX_IMAGE_BYTES = 10
        for response in [image_response(b"short", 11), image_response(b"12345678901")]:
            with self.subTest(response=response):
                get.side_effect = [api_response([candidate()]), response]
                with self.assertRaisesRegex(ArtworkError, "文件过大"):
                    self.client.download_assets(42, self.destination)
                self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.get")
    def test_rejects_untrusted_urls_before_download(self, get):
        self.make_previous()
        for url in ["http://cdn2.steamgriddb.com/a.png", "https://steamgriddb.com.evil.test/a.png", "https://evil.test/a.png", "https://cdn2.steamgriddb.com/a.png?key=private-test-key"]:
            with self.subTest(url=url):
                item = candidate()
                item["url"] = url
                get.side_effect = [api_response([item])]
                with self.assertRaisesRegex(ArtworkError, "可信"):
                    self.client.download_assets(42, self.destination)
                self.assert_previous_unchanged()
        self.assertEqual(get.call_count, 4)

    @patch("game_manager.artwork.requests.get")
    def test_redirects_and_unsupported_formats_fail(self, get):
        self.make_previous()
        redirect = image_response(b"")
        redirect.status_code = 302
        for response, expected in [(redirect, "HTTP 302"), (image_response(image_bytes(image_format="GIF")), "格式不符合")]:
            with self.subTest(expected=expected):
                get.side_effect = [api_response([candidate()]), response]
                with self.assertRaisesRegex(ArtworkError, expected):
                    self.client.download_assets(42, self.destination)
                self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.get")
    def test_manifest_redacts_key_if_server_repeats_it_in_author(self, get):
        get.side_effect = [api_response([candidate(name="private-test-key")]), image_response(image_bytes()), api_response([]), api_response([]), api_response([])]
        manifest = self.client.download_assets(42, self.destination)
        self.assertNotIn("private-test-key", json.dumps(manifest))
        self.assertEqual(manifest["assets"]["cover"]["author"]["name"], "[已隐藏]")

    @patch("game_manager.artwork.requests.get")
    def test_jpeg_cover_is_supported_but_jpeg_logo_is_rejected(self, get):
        self.make_previous()
        get.side_effect = [
            api_response([candidate()]), image_response(image_bytes(image_format="JPEG")),
            api_response([]), api_response([]), api_response([candidate(456)]), image_response(image_bytes((300, 120), "JPEG")),
        ]
        with self.assertRaisesRegex(ArtworkError, "格式不符合"):
            self.client.download_assets(42, self.destination)
        self.assertEqual(get.call_count, 6)
        self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.get")
    def test_animated_png_is_rejected(self, get):
        self.make_previous()
        content = io.BytesIO()
        Image.new("RGB", (600, 900), "red").save(
            content, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (600, 900), "blue")], duration=100, loop=0,
        )
        get.side_effect = [api_response([candidate()]), image_response(content.getvalue())]
        with self.assertRaisesRegex(ArtworkError, "动态图"):
            self.client.download_assets(42, self.destination)
        self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.get")
    def test_publish_failure_rolls_back_previous_directory(self, get):
        self.make_previous()
        get.side_effect = [api_response([]) for _ in range(4)]
        original_rename = Path.rename

        def fail_publish(path, target):
            if path.name.startswith(".artwork-") and "previous" not in path.name:
                raise OSError("模拟目录替换失败")
            return original_rename(path, target)

        with patch.object(Path, "rename", fail_publish):
            with self.assertRaisesRegex(ArtworkError, "无法写入"):
                self.client.download_assets(42, self.destination)
        self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.get")
    def test_malformed_api_data_is_rejected(self, get):
        get.return_value = api_response(None)
        with self.assertRaisesRegex(ArtworkError, "格式不正确"):
            self.client.search("Game")


if __name__ == "__main__":
    unittest.main()
