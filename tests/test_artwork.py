import io
import json
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from game_manager import artwork
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


class CaptureAdapter(requests.adapters.BaseAdapter):
    def __init__(self):
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response._content_consumed = True
        response._content = (json.dumps({"success": True, "data": [{"id": 42, "name": "Game"}]}).encode()
                             if request.url.startswith(SteamGridDB.BASE_URL) else image_bytes())
        return response

    def close(self):
        pass


class SteamGridDBTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "artwork"
        self.client = SteamGridDB("private-test-key")

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def make_previous(self):
        self.destination.mkdir()
        (self.destination / "cover.png").write_bytes(b"previous cover")
        (self.destination / "assets.json").write_text('{"previous": true}', encoding="utf-8")

    def assert_previous_unchanged(self):
        self.assertEqual((self.destination / "cover.png").read_bytes(), b"previous cover")
        self.assertEqual((self.destination / "assets.json").read_text(encoding="utf-8"), '{"previous": true}')
        self.assertEqual(set(self.root.iterdir()), {self.destination})

    @patch("game_manager.artwork.requests.Session.get")
    def test_search_encodes_name_and_returns_game_fields(self, get):
        get.return_value = api_response([{"id": 42, "name": "Game + / Test", "release_date": 100}])
        self.assertEqual(self.client.search("  Game + / Test  "), [{"id": 42, "name": "Game + / Test", "release_date": 100}])
        self.assertEqual(get.call_args.args[0], self.client.BASE_URL + "/search/autocomplete/Game%20%2B%20%2F%20Test")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer private-test-key")
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 30))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        get.return_value.close.assert_called_once()

    @patch("game_manager.artwork.requests.Session.get")
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

    @patch("game_manager.artwork.requests.Session.get")
    def test_empty_results_are_recorded_as_missing(self, get):
        get.side_effect = [api_response([]) for _ in range(4)]
        manifest = self.client.download_assets(42, self.destination)
        self.assertEqual(manifest["assets"], {})
        self.assertEqual(manifest["missing"], ["cover", "wide", "hero", "logo"])
        self.assertEqual(get.call_count, 4)

    @patch("game_manager.artwork.requests.Session.get")
    def test_chooses_first_candidate_with_required_metadata_dimensions(self, get):
        incorrect = candidate(1)
        incorrect.update({"width": 460, "height": 215})
        correct = candidate(2)
        correct.update({"width": 600, "height": 900})
        get.side_effect = [api_response([incorrect, correct]), image_response(image_bytes()), api_response([]), api_response([]), api_response([])]
        manifest = self.client.download_assets(42, self.destination)
        self.assertEqual(manifest["assets"]["cover"]["id"], 2)
        self.assertEqual(get.call_args_list[1].args[0], correct["url"])

    @patch("game_manager.artwork.requests.Session.get")
    def test_missing_key_does_not_make_request(self, get):
        with self.assertRaisesRegex(ArtworkError, "API Key"):
            SteamGridDB("").search("Game")
        get.assert_not_called()

    @patch("game_manager.artwork.requests.Session.get")
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

    @patch("game_manager.artwork.requests.Session.get")
    def test_failed_later_request_preserves_old_images(self, get):
        self.make_previous()
        get.side_effect = [api_response([candidate()]), image_response(image_bytes()), requests.ConnectionError("private-test-key")]
        with self.assertRaisesRegex(ArtworkError, "无法连接"):
            self.client.download_assets(42, self.destination)
        self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.Session.get")
    def test_wrong_dimensions_and_damaged_images_preserve_previous_data(self, get):
        self.make_previous()
        for content, expected in [(image_bytes((600, 899)), "尺寸不正确"), (b"not a valid image", "已损坏")]:
            with self.subTest(expected=expected):
                get.side_effect = [api_response([candidate()]), image_response(content)]
                with self.assertRaisesRegex(ArtworkError, expected):
                    self.client.download_assets(42, self.destination)
                self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.Session.get")
    def test_size_limit_applies_to_header_and_stream(self, get):
        self.make_previous()
        self.client.MAX_IMAGE_BYTES = 10
        for response in [image_response(b"short", 11), image_response(b"12345678901")]:
            with self.subTest(response=response):
                get.side_effect = [api_response([candidate()]), response]
                with self.assertRaisesRegex(ArtworkError, "文件过大"):
                    self.client.download_assets(42, self.destination)
                self.assert_previous_unchanged()

    @patch("game_manager.artwork.requests.Session.get")
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

    @patch("game_manager.artwork.requests.Session.get")
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

    @patch("game_manager.artwork.requests.Session.get")
    def test_manifest_redacts_key_if_server_repeats_it_in_author(self, get):
        get.side_effect = [api_response([candidate(name="private-test-key")]), image_response(image_bytes()), api_response([]), api_response([]), api_response([])]
        manifest = self.client.download_assets(42, self.destination)
        self.assertNotIn("private-test-key", json.dumps(manifest))
        self.assertEqual(manifest["assets"]["cover"]["author"]["name"], "[已隐藏]")

    @patch("game_manager.artwork.requests.Session.get")
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

    @patch("game_manager.artwork.requests.Session.get")
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

    @patch("game_manager.artwork.requests.Session.get")
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

    @patch("game_manager.artwork.requests.Session.get")
    def test_malformed_api_data_is_rejected(self, get):
        get.return_value = api_response(None)
        with self.assertRaisesRegex(ArtworkError, "格式不正确"):
            self.client.search("Game")


class ProxyAndArtworkWorkerTests(unittest.TestCase):
    def test_explicit_proxy_and_direct_mode_ignore_environment_and_system(self):
        environment = {"HTTP_PROXY": "http://environment.invalid:11", "HTTPS_PROXY": "http://environment.invalid:12",
                       "ALL_PROXY": "http://environment.invalid:13", "NO_PROXY": "*"}
        with tempfile.TemporaryDirectory() as directory:
            for proxy_url in ("", "http://127.0.0.1:7890"):
                with self.subTest(proxy_url=proxy_url):
                    client = SteamGridDB("private-test-key", proxy_url=proxy_url)
                    staging = Path(directory) / ("proxy" if proxy_url else "direct")
                    staging.mkdir()
                    adapter = CaptureAdapter()
                    client._session.mount("https://", adapter)
                    try:
                        with patch.dict("os.environ", environment), \
                                patch("requests.sessions.get_environ_proxies", side_effect=AssertionError("读取了环境/系统代理")), \
                                patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("读取了系统认证")):
                            self.assertEqual(client.search("Game"), [{"id": 42, "name": "Game", "release_date": None}])
                            client._download("https://cdn2.steamgriddb.com/grid/1.png", staging, "cover", (600, 900))
                        expected = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
                        self.assertEqual(len(adapter.calls), 2)
                        for request, options in adapter.calls:
                            self.assertEqual(options["proxies"], expected)
                            self.assertTrue(options["verify"])
                        self.assertEqual(adapter.calls[0][0].headers["Authorization"], "Bearer private-test-key")
                        self.assertNotIn("Authorization", adapter.calls[1][0].headers)
                    finally:
                        client.close()

    def test_proxy_validation_normalizes_and_rejects_without_revealing_credentials(self):
        self.assertEqual(SteamGridDB.validate_proxy_url(""), "")
        self.assertEqual(SteamGridDB.validate_proxy_url(" HTTP://ProxyUsernameSecret:Secret@localhost:7890 "),
                         "http://ProxyUsernameSecret:Secret@localhost:7890")
        for value in ("localhost:7890", "https://ProxyUsernameSecret:Secret@localhost:7890", "socks5://localhost:7890",
                      "http://localhost", "http://localhost:0", "http://localhost:65536",
                      "http://localhost:abc", "http://localhost:7890/path", "http://localhost:7890?password=Secret",
                      "http://localhost:7890#Secret", "http://:7890", "http://local\nhost:7890"):
            with self.subTest(value=value):
                try:
                    SteamGridDB.validate_proxy_url(value)
                except ArtworkError as error:
                    details = "".join(traceback.format_exception(error))
                    self.assertNotIn("Secret", details)
                    self.assertNotIn(value, str(error))
                else:
                    self.fail("无效代理地址未被拒绝")

    @patch("game_manager.artwork.requests.Session.get")
    def test_proxy_request_errors_hide_key_and_password(self, get):
        client = SteamGridDB("private-test-key", proxy_url="http://ProxyUsernameSecret:Secret@localhost:7890")
        try:
            for exception in (requests.exceptions.ProxyError, requests.Timeout, requests.ConnectionError):
                for action in ("api", "image"):
                    with self.subTest(exception=exception, action=action):
                        get.side_effect = exception("private-test-key http://ProxyUsernameSecret:Secret@localhost:7890")
                        try:
                            if action == "api":
                                client.search("Game")
                            else:
                                with tempfile.TemporaryDirectory() as directory:
                                    client._download("https://cdn2.steamgriddb.com/grid/1.png", Path(directory), "cover", (600, 900))
                        except ArtworkError as error:
                            details = "".join(traceback.format_exception(error))
                            self.assertNotIn("private-test-key", details)
                            self.assertNotIn("Secret", details)
                            self.assertNotIn("ProxyUsernameSecret", details)
                        else:
                            self.fail("请求失败未被转换为安全错误")
        finally:
            client.close()

    @patch("game_manager.artwork.SteamGridDB")
    def test_worker_search_and_download_send_results_and_close(self, factory):
        client = factory.return_value
        client.search.return_value = [{"id": 42, "name": "Game"}]
        client.download_assets.return_value = {"assets": {}, "missing": []}
        for action, value, destination, result in (
                ("search", "Game", None, client.search.return_value),
                ("download", 42, Path("staging/artwork"), client.download_assets.return_value)):
            with self.subTest(action=action):
                client.reset_mock()
                connection = Mock()
                artwork.artwork_worker(connection, "private-test-key", "http://localhost:7890", action, value, destination)
                factory.assert_called_with("private-test-key", "http://localhost:7890")
                connection.send.assert_called_once_with((True, result))
                connection.close.assert_called_once()
                client.close.assert_called_once()
                if action == "search":
                    client.search.assert_called_once_with(value)
                    client.download_assets.assert_not_called()
                else:
                    client.download_assets.assert_called_once_with(value, destination)
                    client.search.assert_not_called()

    @patch("game_manager.artwork.SteamGridDB")
    def test_worker_safe_and_unknown_errors_close_connection(self, factory):
        client = factory.return_value
        for error, expected in ((ArtworkError("安全错误"), "安全错误"),
                                (RuntimeError("private-test-key Secret"), "图片后台任务失败，请重试。")):
            with self.subTest(error=type(error)):
                client.reset_mock()
                client.search.side_effect = error
                connection = Mock()
                artwork.artwork_worker(connection, "private-test-key", "", "search", "Game")
                connection.send.assert_called_once_with((False, expected))
                client.close.assert_called_once()
                connection.close.assert_called_once()

    @patch("game_manager.artwork.requests.Session.get")
    def test_worker_download_only_writes_given_staging_directory(self, get):
        get.side_effect = [api_response([]) for _ in range(4)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            official = root / "artwork"
            official.mkdir()
            (official / "cover.png").write_bytes(b"previous cover")
            staging = root / "worker" / "artwork"
            connection = Mock()
            artwork.artwork_worker(connection, "private-test-key", "", "download", 42, staging)
            self.assertEqual(connection.send.call_args.args[0][0], True)
            self.assertEqual((official / "cover.png").read_bytes(), b"previous cover")
            self.assertTrue((staging / "assets.json").is_file())
            self.assertFalse((root / "library.json").exists())
            connection.close.assert_called_once()


class SharedNetworkSettingsTests(unittest.TestCase):
    def test_unchecked_global_proxy_ignores_saved_invalid_address_and_environment(self):
        from game_manager import network

        environment = {"HTTP_PROXY": "http://environment.invalid:11", "HTTPS_PROXY": "http://environment.invalid:12",
                       "ALL_PROXY": "http://environment.invalid:13", "NO_PROXY": "*"}
        for settings in ({}, {"proxy_enabled": False, "proxy_url": "not a valid proxy"},
                         {"proxy_enabled": False, "proxy_url": {"invalid": True}},
                         {"proxy_enabled": "true", "proxy_url": "http://localhost:7890"}):
            with self.subTest(settings=settings):
                self.assertEqual(network.settings_proxy_url(settings), "")
                with network.create_session(network.settings_proxy_url(settings)) as session:
                    adapter = CaptureAdapter()
                    session.mount("https://", adapter)
                    with patch.dict("os.environ", environment), \
                            patch("requests.sessions.get_environ_proxies", side_effect=AssertionError("读取了环境/系统代理")):
                        session.get(SteamGridDB.BASE_URL + "/search/autocomplete/Game", allow_redirects=False).close()
                        session.get("https://cdn2.steamgriddb.com/grid/1.png", allow_redirects=False).close()
                    self.assertEqual(len(adapter.calls), 2)
                    self.assertTrue(all(options["proxies"] == {} for _, options in adapter.calls))

    def test_checked_global_proxy_applies_to_api_and_cdn(self):
        from game_manager import network

        settings = {"proxy_enabled": True, "proxy_url": " HTTP://localhost:7890 "}
        proxy_url = network.settings_proxy_url(settings)
        self.assertEqual(proxy_url, "http://localhost:7890")
        client = SteamGridDB("private-test-key", proxy_url)
        adapter = CaptureAdapter()
        client._session.mount("https://", adapter)
        try:
            with tempfile.TemporaryDirectory() as directory:
                client.search("Game")
                client._download("https://cdn2.steamgriddb.com/grid/1.png", Path(directory), "cover", (600, 900))
            self.assertEqual(len(adapter.calls), 2)
            self.assertTrue(all(options["proxies"] == {"http": proxy_url, "https": proxy_url}
                                for _, options in adapter.calls))
            self.assertNotIn("Authorization", adapter.calls[1][0].headers)
        finally:
            client.close()

    def test_checked_global_proxy_requires_valid_nonempty_url_and_preserves_artwork_error(self):
        from game_manager import network

        for value in ("", None, "https://PrivateUser:PrivatePassword@localhost:7890"):
            with self.subTest(value=value), self.assertRaises(network.NetworkError) as raised:
                network.settings_proxy_url({"proxy_enabled": True, "proxy_url": value})
            self.assertNotIn("PrivatePassword", str(raised.exception))
        with self.assertRaises(ArtworkError) as raised:
            SteamGridDB.validate_proxy_url("https://PrivateUser:PrivatePassword@localhost:7890")
        self.assertIsInstance(raised.exception, network.NetworkError)
        self.assertNotIn("PrivatePassword", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
