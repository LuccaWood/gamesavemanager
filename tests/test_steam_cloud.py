from __future__ import annotations

import ctypes
import os
import unittest
import uuid
from unittest import mock

import requests

from game_manager.steam_cloud import SteamCloud, SteamCloudError, _known_folder


class SteamCloudTests(unittest.TestCase):
    def client(self, value):
        response = mock.Mock(status_code=200)
        response.json.return_value = value
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch("game_manager.steam_cloud.create_session", return_value=session):
            client = SteamCloud()
        self.addCleanup(client.close)
        return client, response

    def app(self, rules=None, **ufs):
        if rules is not None:
            ufs["savefiles"] = {str(index): value for index, value in enumerate(rules)}
        return {"status": "success", "data": {"292030": {
            "common": {"name": "The Witcher 3", "gameid": "292030", "type": "Game"}, "ufs": ufs}}}

    def rule(self, root="WinAppDataLocal", path="Game/Saves", **values):
        return {"root": root, "path": path, "pattern": "*", **values}

    def locations(self, rules, **ufs):
        client, response = self.client(self.app(rules, **ufs))
        with mock.patch("game_manager.steam_cloud.sys.platform", "darwin"):
            result = client.find_save_locations("Original Name", 292030)
        response.close.assert_called_once()
        return result

    def test_search_deduplicates_prioritizes_exact_and_preserves_choices(self):
        client, response = self.client({"items": [
            {"id": 2, "name": "Hades II"}, {"id": 1, "name": " Hades "},
            {"id": 1, "name": "Duplicate"}, {"id": True, "name": "Invalid"},
            {"id": 3, "name": ""}, "invalid"]})
        games = client.search_games(" hades ")
        self.assertEqual([item["id"] for item in games], [1, 2])
        self.assertEqual(games[0], {"id": 1, "name": "Hades", "source": "Steam 云存档",
                                   "page_url": "https://steamdb.info/app/1/ufs/"})
        args, kwargs = client._session.get.call_args
        self.assertEqual(args, ("https://store.steampowered.com/api/storesearch",))
        self.assertEqual(kwargs["params"], {"term": "hades", "l": "english", "cc": "us"})
        self.assertIn("timeout", kwargs)
        self.assertNotIn("verify", kwargs)
        response.close.assert_called_once()

    def test_search_limits_results_and_accepts_empty_list(self):
        client, _ = self.client({"items": [{"id": number, "name": f"Game {number}"} for number in range(1, 15)]})
        self.assertEqual(len(client.search_games("Game")), 10)
        self.assertEqual(client.search_games("Game 14")[0]["id"], 14)
        self.assertEqual(self.client({"items": []})[0].search_games("Game"), [])

    def test_search_invalid_response_and_blank_name(self):
        for value in (None, [], {}, {"items": {}}, {"items": None}):
            with self.subTest(value=value), self.assertRaises(SteamCloudError):
                self.client(value)[0].search_games("Game")
        for name in (None, "", " "):
            client, _ = self.client({"items": []})
            with self.subTest(name=name), self.assertRaises(SteamCloudError):
                client.search_games(name)
            client._session.get.assert_not_called()

    def test_only_selected_app_id_is_queried(self):
        client, _ = self.client(self.app([self.rule()]))
        result = client.find_save_locations("Original Name", 292030)
        self.assertEqual(result[0]["title"], "The Witcher 3")
        self.assertEqual(result[0]["source"], "Steam 云存档")
        self.assertEqual(result[0]["page_url"], "https://steamdb.info/app/292030/ufs/")
        self.assertEqual(client._session.get.call_args.args, ("https://api.steamcmd.net/v1/info/292030",))
        self.assertEqual(client._session.get.call_count, 1)
        for app_id in (0, -1, True, "292030", None, 4294967296):
            client, _ = self.client(self.app())
            with self.subTest(app_id=app_id), self.assertRaises(SteamCloudError):
                client.find_save_locations("Game", app_id)
            client._session.get.assert_not_called()

    def test_cloud_quota_without_savefiles_and_missing_ufs_return_empty(self):
        for value in (self.app(quota="1000"), self.app([])):
            self.assertEqual(self.client(value)[0].find_save_locations("Game", 292030), [])
        value = self.app()
        del value["data"]["292030"]["ufs"]
        self.assertEqual(self.client(value)[0].find_save_locations("Game", 292030), [])

    def test_invalid_app_payload_and_mismatched_game_id_fail(self):
        invalid = [None, [], {}, {"status": "failed", "data": {}}, {"status": "success", "data": []},
                   {"status": "success", "data": {"292030": {}}}]
        for common in ({"name": "", "type": "Game"}, {"name": None, "type": "Game"},
                       {"name": "Game", "gameid": "1", "type": "Game"},
                       {"name": "Game", "gameid": True, "type": "Game"}, {"name": "Tool", "type": "Tool"}):
            invalid.append({"status": "success", "data": {"292030": {"common": common}}})
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(SteamCloudError):
                self.client(payload)[0].find_save_locations("Game", 292030)
        value = self.app([self.rule()])
        del value["data"]["292030"]["common"]["gameid"]
        self.assertTrue(self.client(value)[0].find_save_locations("Game", 292030))

    def test_windows_and_all_platform_rules_exclude_mac_and_linux(self):
        rules = [self.rule(path="Windows", platforms={"1": "Windows"}),
                 self.rule(path="All", platforms={"0": "All OSes"}),
                 self.rule(path="Linux", platforms={"1": "Linux"}),
                 self.rule("MacHome", "Mac", platforms={"1": "MacOS"}),
                 self.rule("LinuxHome", "Linux", platforms={"1": "All"})]
        self.assertEqual([item["path"] for item in self.locations(rules)],
                         [r"%LOCALAPPDATA%\Windows", r"%LOCALAPPDATA%\All"])

    def test_real_leading_slash_paths_and_patterns_are_separate(self):
        result = self.locations([self.rule("WinMyDocuments", "/The Witcher 3/gamesaves/"),
                                 self.rule("WinMyDocuments", "/The Witcher 3/", pattern="user.settings")])
        self.assertEqual([item["path"] for item in result],
                         [r"<documents>\The Witcher 3\gamesaves", r"<documents>\The Witcher 3"])
        self.assertIn("user.settings", result[1]["label"])
        self.assertFalse(any(item["resolved"] for item in result))

    def test_redirected_windows_known_folders_are_used(self):
        roots = ("WinMyDocuments", "WinAppDataLocal", "WinAppDataRoaming", "WinAppDataLocalLow", "WinSavedGames")
        client, _ = self.client(self.app([self.rule(root, "Game/Saves") for root in roots]))
        with mock.patch("game_manager.steam_cloud.sys.platform", "win32"), mock.patch(
                "game_manager.steam_cloud._known_folder", side_effect=lambda root: rf"D:\Redirected\{root}") as folder:
            result = client.find_save_locations("Game", 292030)
        self.assertEqual(folder.call_count, len(roots))
        self.assertEqual([item["path"] for item in result], [rf"D:\Redirected\{root}\Game\Saves" for root in roots])
        self.assertTrue(all(item["resolved"] for item in result))

    def test_missing_known_folder_does_not_guess_windows_profile_location(self):
        client, _ = self.client(self.app([self.rule("WinMyDocuments"), self.rule("WinSavedGames")]))
        with mock.patch("game_manager.steam_cloud.sys.platform", "win32"), mock.patch(
                "game_manager.steam_cloud._known_folder", return_value=None):
            result = client.find_save_locations("Game", 292030)
        self.assertEqual([item["path"] for item in result], [r"<documents>\Game\Saves", r"<saved-games>\Game\Saves"])
        self.assertFalse(any(item["resolved"] for item in result))

    def test_known_folder_uses_documents_guid_and_frees_allocated_memory(self):
        buffer = ctypes.create_unicode_buffer(r"D:\OneDrive\Documents")
        shell, ole = mock.Mock(), mock.Mock()

        def lookup(folder_id, flags, token, result):
            self.assertEqual(ctypes.string_at(folder_id, 16),
                             uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le)
            self.assertEqual(flags, 0)
            self.assertIsNone(token)
            ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            return 0

        shell.SHGetKnownFolderPath.side_effect = lookup
        with mock.patch("game_manager.steam_cloud.ctypes.WinDLL", side_effect=[shell, ole], create=True):
            self.assertEqual(_known_folder("WinMyDocuments"), r"D:\OneDrive\Documents")
        ole.CoTaskMemFree.assert_called_once()
        self.assertEqual(ole.CoTaskMemFree.call_args.args[0].value, ctypes.addressof(buffer))

    def test_known_folder_native_failure_returns_no_guessed_path(self):
        shell, ole = mock.Mock(), mock.Mock()
        shell.SHGetKnownFolderPath.return_value = -1
        with mock.patch("game_manager.steam_cloud.ctypes.WinDLL", side_effect=[shell, ole], create=True):
            self.assertIsNone(_known_folder("WinSavedGames"))
        ole.CoTaskMemFree.assert_not_called()
        with mock.patch("game_manager.steam_cloud.ctypes.WinDLL", side_effect=OSError("secret"), create=True):
            self.assertIsNone(_known_folder("WinMyDocuments"))

    def test_account_and_install_placeholders_remain_unresolved(self):
        result = self.locations([self.rule(path="Game/{64BitSteamID}/Saves"),
                                 self.rule(path="Game/{Steam3AccountID}"),
                                 self.rule("AppInstallDir", "Saves"),
                                 self.rule("SteamCloudDocuments", "Game")])
        self.assertEqual(len(result), 4)
        self.assertFalse(any(item["resolved"] for item in result))
        self.assertIn("{64BitSteamID}", result[0]["path"])
        self.assertEqual(result[2]["path"], r"<game-folder>\Saves")

    def test_unsafe_relative_paths_and_broad_roots_are_never_resolved(self):
        paths = ("", ".", "..", "../Other", "Game/../../Other", "C:/Other", "//server/share",
                 r"\Other", "Game/NUL", "Game/*", "Game:stream", "Game/trailing.", "Game\x00bad")
        result = self.locations([self.rule(path=path) for path in paths])
        self.assertEqual(len(result), len(paths))
        self.assertFalse(any(item["resolved"] for item in result))

    def test_directory_with_extension_is_not_mistaken_for_filename(self):
        result = self.locations([self.rule(path="Game.vdf"), self.rule(path="Game/Version.1/Saves")])
        self.assertEqual(result[0]["path"], r"%LOCALAPPDATA%\Game.vdf")
        self.assertTrue(all(item["resolved"] for item in result))

    def test_repeated_paths_deduplicate_but_keep_distinct_patterns(self):
        result = self.locations([self.rule(), self.rule(), self.rule(pattern="*.sav")])
        self.assertEqual(len(result), 2)
        self.assertIn("*.sav", result[1]["label"])

    def test_mac_override_does_not_change_windows_path(self):
        overrides = {"1": {"os": "MacOS", "oscompare": "=", "root": "WinAppDataLocal",
                            "useinstead": "MacAppSupport", "pathtransforms": {
                                "0": {"find": "Game", "replace": "Mac Game"}}}}
        result = self.locations([self.rule()], rootoverrides=overrides)
        self.assertEqual(result[0]["path"], r"%LOCALAPPDATA%\Game\Saves")
        self.assertTrue(result[0]["resolved"])

    def test_simple_windows_root_override_maps_all_platform_rule(self):
        overrides = {"0": {"os": "Windows", "oscompare": "=", "root": "MacHome",
                            "useinstead": "WinAppDataLocal"}}
        result = self.locations([self.rule("MacHome", "Game/Saves", platforms={"0": "All"})],
                                rootoverrides=overrides)
        self.assertEqual(result[0]["path"], r"%LOCALAPPDATA%\Game\Saves")
        self.assertTrue(result[0]["resolved"])

    def test_complex_or_ambiguous_windows_override_is_unresolved(self):
        transforms = {"0": {"os": "Windows", "root": "WinAppDataLocal", "useinstead": "WinMyDocuments",
                            "pathtransforms": {"0": {"find": "Game", "replace": "Other"}}}}
        ambiguous = {"0": {"os": "Windows", "root": "WinAppDataLocal", "useinstead": "WinMyDocuments"},
                     "1": {"os": "Windows", "root": "WinAppDataLocal", "useinstead": "WinSavedGames"}}
        not_mac = {"0": {"os": "MacOS", "oscompare": "!=", "root": "WinAppDataLocal", "useinstead": "WinSavedGames"}}
        for overrides in (transforms, ambiguous, not_mac, []):
            with self.subTest(overrides=overrides):
                result = self.locations([self.rule()], rootoverrides=overrides)
                self.assertEqual(len(result), 1)
                self.assertFalse(result[0]["resolved"])
                self.assertIn("覆盖", result[0]["label"])

    def test_http_json_and_connection_errors_close_responses_without_secrets(self):
        for status in (403, 429, 500):
            client, response = self.client({})
            response.status_code = status
            with self.subTest(status=status), self.assertRaisesRegex(SteamCloudError, str(status)):
                client.find_save_locations("Game", 292030)
            response.json.assert_not_called()
            response.close.assert_called_once()
        client, response = self.client({})
        response.json.side_effect = ValueError("secret")
        with self.assertRaises(SteamCloudError) as caught:
            client.search_games("Game")
        self.assertNotIn("secret", str(caught.exception))
        response.close.assert_called_once()
        for error in (requests.Timeout("user:secret@proxy"), requests.ConnectionError("user:secret@proxy")):
            client, _ = self.client({})
            client._session.get.side_effect = error
            with self.subTest(error=error), self.assertRaises(SteamCloudError) as caught:
                client.search_games("Game")
            self.assertNotIn("secret", str(caught.exception))

    def test_shared_proxy_session_and_explicit_close(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://environment:1234"}):
            for proxy, expected in (("", {}), ("socket://127.0.0.1:7890", {
                    "http": "socks5h://127.0.0.1:7890", "https": "socks5h://127.0.0.1:7890"})):
                client = SteamCloud(proxy)
                try:
                    self.assertEqual(client._session.proxies, expected)
                    self.assertFalse(client._session.trust_env)
                finally:
                    client.close()
        session = mock.Mock()
        with mock.patch("game_manager.steam_cloud.create_session", return_value=session):
            client = SteamCloud("http://127.0.0.1:7890")
        client.close()
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
