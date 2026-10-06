from __future__ import annotations

import os
import unittest
from unittest import mock

import requests

from game_manager.pcgamingwiki import PCGamingWiki, PCGamingWikiError, _lookup_steam_cloud, pcgw_worker
from game_manager.network import NetworkError


HTML = r'''
<h3><span id="Configuration_file.28s.29_location">Configuration file(s) location</span></h3>
<table><tr><td>Windows</td><td><code>%APPDATA%\Wrong\Config</code></td></tr></table>
<h3><span class="mw-headline" id="Save_game_data_location">Save game data location</span></h3>
<table class="wikitable"><tr><th>System</th><th>Location</th></tr>
<tr><td>Windows</td><td><code><span title="tooltip">%USERPROFILE%</span>\Documents\Saved Games\Hades\</code><sup>[1]</sup></td></tr>
<tr><td>GOG.com</td><td><code>%LOCALAPPDATA%\Remedy\Control\Default-Generic-User\</code><br>
<code>%USERPROFILE%\Documents\My Games\Control\Saves\</code></td></tr>
<tr><td>Steam</td><td><code>&lt;Steam-folder&gt;\userdata\&lt;user-id&gt;\870780\remote\</code></td></tr>
<tr><td>Epic Games Launcher</td><td><code>%LOCALAPPDATA%\Remedy\Control\Default-Epic-User\</code></td></tr>
<tr><td>Microsoft Store</td><td><code>%LOCALAPPDATA%\Packages\Hades\SystemAppData\wgs\</code></td></tr>
<tr><td>macOS (OS X)</td><td><code>$HOME/Library/Application Support/Hades</code></td></tr>
<tr><td>Steam Play (Linux)</td><td><code>&lt;SteamLibrary-folder&gt;/steamapps/compatdata/1/pfx/</code></td></tr>
</table><h3 id="Save_game_cloud_syncing">Save game cloud syncing</h3>
<table><tr><td>Windows</td><td><code>%APPDATA%\Wrong\Cloud</code></td></tr></table>
'''


class PCGamingWikiTests(unittest.TestCase):
    def client(self, *values):
        client = PCGamingWiki()
        self.addCleanup(client.close)
        responses = []
        for value in values:
            response = mock.Mock(status_code=200)
            response.json.return_value = value
            responses.append(response)
        client._session.get = mock.Mock(side_effect=responses)
        return client

    def parsed(self, html=HTML, title="Hades"):
        return {"parse": {"title": title, "text": html}}

    def test_windows_stores_multiple_paths_section_and_variables(self):
        client = self.client(self.parsed())
        with mock.patch.dict(os.environ, {"USERPROFILE": "MacHome", "LOCALAPPDATA": "MacData"}):
            candidates = client.find_save_locations(" Hades ")
        self.assertEqual(len(candidates), 6)
        self.assertEqual(candidates[0]["path"], r"%USERPROFILE%\Documents\Saved Games\Hades")
        self.assertTrue(candidates[0]["resolved"])
        self.assertEqual(candidates[0]["page_url"], "https://www.pcgamingwiki.com/wiki/Hades")
        self.assertEqual([item["label"] for item in candidates[:3]], ["Windows", "GOG.com", "GOG.com"])
        steam = next(item for item in candidates if item["label"].startswith("Steam"))
        self.assertFalse(steam["resolved"])
        self.assertIn("<user-id>", steam["path"])
        self.assertTrue(all("Wrong" not in item["path"] and "Linux" not in item["label"] and "macOS" not in item["label"] for item in candidates))
        request = client._session.get.call_args
        self.assertEqual(request.kwargs["params"]["page"], "Hades")
        self.assertEqual(request.kwargs["params"]["prop"], "text")
        self.assertEqual(request.kwargs["timeout"], (5, 20))
        self.assertIn("GameSaveManager/", request.kwargs["headers"]["User-Agent"])

    def test_exact_search_fallback_and_redirect_title(self):
        client = self.client({"error": {"code": "missingtitle"}},
                             {"query": {"search": [{"title": "Hades II"}, {"title": "Hades"}]}}, self.parsed(title="Hades"))
        self.assertTrue(client.find_save_locations("hades"))
        calls = client._session.get.call_args_list
        self.assertEqual(calls[1].kwargs["params"]["list"], "search")
        self.assertEqual(calls[2].kwargs["params"]["page"], "Hades")
        client = self.client(self.parsed(title="Canonical Game"))
        self.assertEqual(client.find_save_locations("Official Redirect")[0]["title"], "Canonical Game")

    def test_fuzzy_search_never_selects_a_different_game(self):
        client = self.client({"error": {"code": "missingtitle"}}, {"query": {"search": [{"title": "Hades II"}]}})
        with self.assertRaisesRegex(PCGamingWikiError, "精确匹配"):
            client.find_save_locations("Hade")
        self.assertEqual(client._session.get.call_count, 2)

    def test_search_returns_choices_with_exact_title_first_and_valid_main_articles_only(self):
        client = self.client({"query": {"search": [
            {"ns": 0, "pageid": 12, "title": "Hades II"},
            {"ns": 0, "pageid": 11, "title": "Hades"},
            {"ns": 0, "pageid": 12, "title": "Duplicate ID"},
            {"ns": 0, "pageid": 13, "title": "hades II"},
            {"ns": 1, "pageid": 14, "title": "Talk:Hades"},
            {"ns": 0, "pageid": 0, "title": "Invalid ID"},
            {"ns": 0, "pageid": True, "title": "Boolean ID"},
            {"ns": 0, "pageid": "15", "title": "Text ID"},
            {"ns": 0, "pageid": 16, "title": " "},
            {"ns": 0, "pageid": 17, "title": None},
            "invalid item",
        ]}})
        candidates = client.search_games(" hades ")
        self.assertEqual([item["title"] for item in candidates], ["Hades", "Hades II"])
        self.assertEqual(candidates[1]["page_url"], "https://www.pcgamingwiki.com/wiki/Hades_II")
        params = client._session.get.call_args.kwargs["params"]
        self.assertEqual(params["action"], "query")
        self.assertEqual(params["list"], "search")
        self.assertEqual(params["srsearch"], "hades")
        self.assertEqual(params["srnamespace"], 0)
        self.assertEqual(params["srlimit"], 10)
        self.assertEqual(params["srprop"], "")
        self.assertEqual(client._session.get.call_count, 1)

    def test_search_limit_empty_and_invalid_responses(self):
        matches = [{"ns": 0, "pageid": number, "title": f"Game {number}"} for number in range(1, 15)]
        self.assertEqual(len(self.client({"query": {"search": matches}}).search_games("Game")), 10)
        self.assertEqual(self.client({"query": {"search": []}}).search_games("Game"), [])
        for value in ({}, {"query": []}, {"query": {"search": {}}}, {"error": {"info": "secret"}}):
            with self.subTest(value=value), self.assertRaises(PCGamingWikiError) as caught:
                self.client(value).search_games("Game")
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(PCGamingWikiError, "英文名"):
            self.client().search_games(" ")

    def test_selected_page_id_is_parsed_without_search_or_title_fallback(self):
        client = self.client(self.parsed(title="Renamed Article"))
        candidates = client.find_save_locations("Original Title", page_id=42)
        self.assertEqual(candidates[0]["title"], "Renamed Article")
        params = client._session.get.call_args.kwargs["params"]
        self.assertEqual(params["pageid"], 42)
        self.assertNotIn("page", params)
        for error in ({"code": "missingtitle"}, {"code": "invalidpageid"}):
            client = self.client({"error": error})
            with self.subTest(error=error), self.assertRaises(PCGamingWikiError):
                client.find_save_locations("Old Title", page_id=42)
            self.assertEqual(client._session.get.call_count, 1)
        for page_id in (0, -1, True, "42"):
            client = self.client()
            with self.subTest(page_id=page_id), self.assertRaises(PCGamingWikiError):
                client.find_save_locations("Title", page_id=page_id)
            client._session.get.assert_not_called()

    def test_worker_multiple_matches_waits_for_selection_even_with_exact_match(self):
        connection = mock.Mock()
        client = mock.Mock()
        client.search_games.return_value = [{"pageid": 11, "title": "Hades"}, {"pageid": 12, "title": "Hades II"}]
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client):
            pcgw_worker(connection, "", "Hades")
        connection.send.assert_called_once_with((True, {"games": client.search_games.return_value}))
        client.find_save_locations.assert_not_called()
        client.close.assert_called_once()
        connection.close.assert_called_once()

    def test_worker_empty_unique_and_selected_matches(self):
        connection = mock.Mock()
        client = mock.Mock()
        client.search_games.return_value = []
        empty = {"locations": [], "source": "Steam 云存档"}
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client), \
                mock.patch("game_manager.pcgamingwiki._lookup_steam_cloud", return_value=empty) as fallback:
            pcgw_worker(connection, "", "Hades")
        connection.send.assert_called_once_with((True, empty))
        fallback.assert_called_once_with("", "Hades")
        client.find_save_locations.assert_not_called()
        connection.reset_mock()
        candidate = {"pageid": 12, "title": "Hades II"}
        client.search_games.return_value = [candidate]
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client):
            pcgw_worker(connection, "", "Hade")
        client.find_save_locations.assert_called_once_with("Hades II", page_id=12)
        connection.send.assert_called_once_with((True, client.find_save_locations.return_value))
        client.reset_mock()
        connection.reset_mock()
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client):
            pcgw_worker(connection, "", candidate)
        client.search_games.assert_not_called()
        client.find_save_locations.assert_called_once_with("Hades II", page_id=12)
        connection.send.assert_called_once_with((True, client.find_save_locations.return_value))

    def test_worker_access_failure_uses_steam_fallback_with_same_proxy(self):
        for error in (PCGamingWikiError("PCGamingWiki 拒绝访问（HTTP 403）。"),
                      PCGamingWikiError("PCGamingWiki 查询超时。")):
            connection, client = mock.Mock(), mock.Mock()
            client.search_games.side_effect = error
            result = [{"path": r"%LOCALAPPDATA%\Game", "resolved": True, "source": "Steam 云存档"}]
            with self.subTest(error=error), mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client), \
                    mock.patch("game_manager.pcgamingwiki._lookup_steam_cloud", return_value=result) as fallback:
                pcgw_worker(connection, "http://127.0.0.1:7890", "Game")
            fallback.assert_called_once_with("http://127.0.0.1:7890", "Game")
            connection.send.assert_called_once_with((True, result))
            client.close.assert_called_once()
            connection.close.assert_called_once()

    def test_worker_missing_windows_paths_uses_selected_article_title_for_fallback(self):
        connection, client = mock.Mock(), mock.Mock()
        client.find_save_locations.return_value = []
        article = {"title": "Selected Game", "pageid": 42}
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client), \
                mock.patch("game_manager.pcgamingwiki._lookup_steam_cloud", return_value=[]) as fallback:
            pcgw_worker(connection, "", article)
        fallback.assert_called_once_with("", "Selected Game")
        client.find_save_locations.assert_called_once_with("Selected Game", page_id=42)

    def test_worker_successful_wiki_result_does_not_query_steam(self):
        connection, client = mock.Mock(), mock.Mock()
        client.search_games.return_value = [{"title": "Game", "pageid": 42}]
        client.find_save_locations.return_value = [{"path": r"%APPDATA%\Game", "resolved": True}]
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client), \
                mock.patch("game_manager.pcgamingwiki._lookup_steam_cloud") as fallback:
            pcgw_worker(connection, "", "Game")
        fallback.assert_not_called()
        connection.send.assert_called_once_with((True, client.find_save_locations.return_value))

    def test_http_403_falls_back_through_real_clients_and_returns_steam_rules(self):
        wiki_session, steam_session, connection = mock.Mock(), mock.Mock(), mock.Mock()
        wiki_session.get.return_value = mock.Mock(status_code=403)
        search, info = mock.Mock(status_code=200), mock.Mock(status_code=200)
        search.json.return_value = {"items": [{"id": 1145360, "name": "Hades"}]}
        info.json.return_value = {"status": "success", "data": {"1145360": {
            "common": {"name": "Hades", "gameid": "1145360", "type": "Game"},
            "ufs": {"savefiles": {"0": {"root": "WinAppDataLocal", "path": "Hades/Saves", "pattern": "*.sav"}}}}}}
        steam_session.get.side_effect = [search, info]
        with mock.patch("game_manager.pcgamingwiki.create_session", return_value=wiki_session) as wiki_factory, \
                mock.patch("game_manager.steam_cloud.create_session", return_value=steam_session) as steam_factory, \
                mock.patch("game_manager.steam_cloud.sys.platform", "darwin"):
            pcgw_worker(connection, "http://localhost:7890", "Hades")
        wiki_factory.assert_called_once_with("http://localhost:7890")
        steam_factory.assert_called_once_with("http://localhost:7890")
        ok, result = connection.send.call_args.args[0]
        self.assertTrue(ok)
        self.assertEqual(result[0]["source"], "Steam 云存档")
        self.assertEqual(result[0]["path"], r"%LOCALAPPDATA%\Hades\Saves")
        self.assertTrue(result[0]["resolved"])
        self.assertEqual(wiki_session.get.call_count, 1)
        self.assertEqual(steam_session.get.call_count, 2)
        search.close.assert_called_once()
        info.close.assert_called_once()
        wiki_session.close.assert_called_once()
        steam_session.close.assert_called_once()
        connection.close.assert_called_once()

    def test_worker_steam_selection_bypasses_wiki_and_preserves_id(self):
        connection = mock.Mock()
        candidate = {"name": "Game", "id": 1145360, "source": "Steam 云存档"}
        result = [{"path": r"%APPDATA%\Game", "source": "Steam 云存档", "resolved": True}]
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki") as wiki, \
                mock.patch("game_manager.pcgamingwiki._lookup_steam_cloud", return_value=result) as fallback:
            pcgw_worker(connection, "http://localhost:7890", candidate)
        wiki.assert_not_called()
        fallback.assert_called_once_with("http://localhost:7890", candidate)
        connection.send.assert_called_once_with((True, result))
        connection.close.assert_called_once()

    def test_worker_reports_both_failed_sources_without_retrying(self):
        from game_manager.network import NetworkError
        connection, client = mock.Mock(), mock.Mock()
        client.search_games.side_effect = PCGamingWikiError("PCGamingWiki 拒绝访问（HTTP 403）。")
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client), \
                mock.patch("game_manager.pcgamingwiki._lookup_steam_cloud", side_effect=NetworkError("Steam 备用查询超时。")) as fallback:
            pcgw_worker(connection, "", "Game")
        ok, message = connection.send.call_args.args[0]
        self.assertFalse(ok)
        self.assertIn("403", message)
        self.assertIn("Steam 备用查询超时", message)
        fallback.assert_called_once()
        client.close.assert_called_once()
        connection.close.assert_called_once()

    def test_steam_fallback_waits_for_game_choice_and_queries_only_selected_id(self):
        client = mock.Mock()
        client.search_games.return_value = [{"id": 11, "name": "Game", "source": "Steam 云存档"},
                                           {"id": 12, "name": "Game II", "source": "Steam 云存档"}]
        with mock.patch("game_manager.steam_cloud.SteamCloud", return_value=client) as factory:
            result = _lookup_steam_cloud("https://localhost:7890", "Game")
        factory.assert_called_once_with("https://localhost:7890")
        self.assertEqual(result, {"games": client.search_games.return_value, "source": "Steam 云存档"})
        client.find_save_locations.assert_not_called()
        client.close.assert_called_once()
        client.reset_mock()
        client.find_save_locations.return_value = [{"path": r"%APPDATA%\GameII", "source": "Steam 云存档"}]
        with mock.patch("game_manager.steam_cloud.SteamCloud", return_value=client):
            result = _lookup_steam_cloud("", {"id": 12, "name": "Game II", "source": "Steam 云存档"})
        client.search_games.assert_not_called()
        client.find_save_locations.assert_called_once_with("Game II", 12)
        self.assertEqual(result, client.find_save_locations.return_value)
        client.close.assert_called_once()

    def test_steam_fallback_unique_empty_and_error_close_client(self):
        client = mock.Mock()
        client.search_games.return_value = [{"id": 11, "name": "Game", "source": "Steam 云存档"}]
        client.find_save_locations.return_value = []
        with mock.patch("game_manager.steam_cloud.SteamCloud", return_value=client):
            self.assertEqual(_lookup_steam_cloud("", "Game"), {"locations": [], "source": "Steam 云存档"})
        client.find_save_locations.assert_called_once_with("Game", 11)
        client.reset_mock()
        client.search_games.return_value = []
        with mock.patch("game_manager.steam_cloud.SteamCloud", return_value=client):
            self.assertEqual(_lookup_steam_cloud("", "Game"), {"locations": [], "source": "Steam 云存档"})
        client.find_save_locations.assert_not_called()
        client.reset_mock()
        client.search_games.side_effect = PCGamingWikiError("模拟网络错误")
        with mock.patch("game_manager.steam_cloud.SteamCloud", return_value=client), self.assertRaises(PCGamingWikiError):
            _lookup_steam_cloud("", "Game")
        client.close.assert_called_once()

    def test_worker_invalid_selected_candidate_never_falls_back_to_title_search(self):
        for candidate in ({"title": "Game"}, {"title": "Game", "pageid": "42"}, {"title": "", "pageid": 42}):
            connection = mock.Mock()
            client = mock.Mock()
            with self.subTest(candidate=candidate), mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client):
                pcgw_worker(connection, "", candidate)
            self.assertFalse(connection.send.call_args.args[0][0])
            client.search_games.assert_not_called()
            client.find_save_locations.assert_not_called()
            connection.close.assert_called_once()

    def test_files_registry_placeholders_and_dangerous_roots(self):
        paths = [r"%APPDATA%\Game\slot.sav", r"%LOCALAPPDATA%\Game\*.sav", r"HKCU\Software\Game",
                 r"<path-to-game>\Saves", "C:\\", r"%USERPROFILE%", r"%USERPROFILE%\Documents",
                 r"%LOCALAPPDATA%", r"%APPDATA%\..\Other", r"%UNKNOWN%\Game", r"%APPDATA%\Game:stream"]
        rows = "".join(f"<tr><td>Windows</td><td><code>{path.replace('<', '&lt;').replace('>', '&gt;')}</code></td></tr>" for path in paths)
        client = self.client(self.parsed(f'<h3 id="Save_game_data_location">Save game data location</h3><table>{rows}</table>'))
        candidates = client.find_save_locations("Game")
        self.assertEqual(candidates[0]["path"], r"%APPDATA%\Game")
        self.assertTrue(candidates[0]["resolved"])
        self.assertIn("文件所在目录", candidates[0]["label"])
        self.assertEqual(candidates[1]["path"], r"%LOCALAPPDATA%\Game")
        self.assertTrue(candidates[1]["resolved"])
        self.assertTrue(all(not item["resolved"] for item in candidates[2:]))

    def test_rowspan_modern_heading_blocks_and_duplicate_candidates(self):
        html = r'''<div class="mw-heading mw-heading3"><h3 id="Save_game_data_location">Save game data location</h3></div>
<table><tr><td rowspan="2">Windows</td><td><p><code>%APPDATA%\Game\Save\</code></p></td></tr>
<tr><td><code>%APPDATA%\Game\Save\</code><br><code>%APPDATA%\Game\Other\</code></td></tr></table>'''
        client = self.client(self.parsed(html))
        candidates = client.find_save_locations("Game")
        self.assertEqual([item["path"] for item in candidates], [r"%APPDATA%\Game\Save", r"%APPDATA%\Game\Other"])

    def test_missing_save_section_or_non_windows_returns_empty(self):
        for html in ('<h3>Configuration file(s) location</h3><table><tr><td>Windows</td><td>%APPDATA%\\Game</td></tr></table>',
                     '<h3 id="Save_game_data_location">Save game data location</h3><table><tr><td>Linux</td><td>$HOME/.game</td></tr></table>'):
            with self.subTest(html=html):
                self.assertEqual(self.client(self.parsed(html)).find_save_locations("Game"), [])

    def test_visible_notes_are_not_appended_to_code_paths(self):
        html = r'''<h3 id="Save_game_data_location">Save game data location</h3><table><tr><td>Windows</td>
<td><code>%APPDATA%\Game\Save\</code> contains the saved profiles.<br>
<code>%LOCALAPPDATA%\Game\Save\</code><sup><a>reference</a></sup></td></tr></table>'''
        candidates = self.client(self.parsed(html)).find_save_locations("Game")
        self.assertEqual([item["path"] for item in candidates], [r"%APPDATA%\Game\Save", r"%LOCALAPPDATA%\Game\Save"])
        self.assertTrue(all(item["resolved"] for item in candidates))

    def test_invalid_names_and_common_home_roots_remain_unresolved(self):
        paths = [r'%APPDATA%\Game\bad"name', r"%APPDATA%\Game\NUL", r"%APPDATA%\Game\COM1.txt",
                 r"C:\Users\Player", r"C:\Users\Player\Documents", r"C:\Windows", r"%USERPROFILE%\AppData\Roaming",
                 r"%APPDATA%\%BAD\Game", r"%APPDATA%\Game\unrecognized.file", r"C:\Users\Player\slot.sav",
                 r"%USERPROFILE%\Documents\slot.sav"]
        html = '<h3 id="Save_game_data_location">Save game data location</h3><table>' + ''.join(
            f'<tr><td>Windows</td><td><code>{path}</code></td></tr>' for path in paths) + '</table>'
        candidates = self.client(self.parsed(html)).find_save_locations("Game")
        self.assertEqual(len(candidates), len(paths) - 1)
        self.assertTrue(all(not item["resolved"] for item in candidates))

    def test_http_and_json_errors_are_chinese_and_do_not_expose_proxy_credentials(self):
        for status, message in ((403, "403"), (429, "429"), (500, "500")):
            client = self.client()
            client._session.get = mock.Mock(return_value=mock.Mock(status_code=status))
            with self.subTest(status=status), self.assertRaisesRegex(PCGamingWikiError, message):
                client.find_save_locations("Game")
        for error in (requests.Timeout("http://user:secret@proxy:1234"), requests.ConnectionError("secret")):
            client = self.client()
            client._session.get = mock.Mock(side_effect=error)
            with self.assertRaises(PCGamingWikiError) as caught:
                client.find_save_locations("Game")
            self.assertNotIn("secret", str(caught.exception))
        for value in ({}, {"error": {"code": "permissiondenied", "info": "secret"}}, {"error": []}, {"parse": {"title": "Game", "text": []}}):
            with self.subTest(value=value), self.assertRaises(PCGamingWikiError):
                self.client(value).find_save_locations("Game")

    def test_shared_proxy_session_and_blank_name(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://environment:1234", "HTTP_PROXY": "http://environment:1234"}):
            client = PCGamingWiki()
            self.addCleanup(client.close)
            self.assertFalse(client._session.trust_env)
            self.assertEqual(client._session.proxies, {})
            proxy = PCGamingWiki("http://127.0.0.1:7890")
            self.addCleanup(proxy.close)
            self.assertFalse(proxy._session.trust_env)
            self.assertEqual(proxy._session.proxies, {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})
        with self.assertRaisesRegex(PCGamingWikiError, "英文名"):
            client.find_save_locations(" ")

    def test_legacy_proxy_is_rejected_before_wiki_request(self):
        for proxy in ("socket://PrivateUser:PrivatePassword@localhost:7890",
                      "socks5h://PrivateUser:PrivatePassword@localhost:7890"):
            with self.subTest(proxy=proxy), mock.patch("requests.Session.get") as get:
                with self.assertRaisesRegex(NetworkError, "HTTP.*HTTPS") as caught:
                    PCGamingWiki(proxy)
                self.assertNotIn("PrivateUser", str(caught.exception))
                self.assertNotIn("PrivatePassword", str(caught.exception))
                get.assert_not_called()

    def test_worker_success_failure_and_cleanup(self):
        connection = mock.Mock()
        client = mock.Mock()
        client.search_games.return_value = [{"title": "Game", "pageid": 42}]
        client.find_save_locations.return_value = [{"path": "%APPDATA%\\Game", "resolved": True}]
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client) as factory:
            pcgw_worker(connection, "http://127.0.0.1:7890", "Game")
        factory.assert_called_once_with("http://127.0.0.1:7890")
        connection.send.assert_called_once_with((True, client.find_save_locations.return_value))
        client.close.assert_called_once()
        connection.close.assert_called_once()
        connection.reset_mock()
        client.close.side_effect = RuntimeError("simulated client cleanup failure")
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", return_value=client), self.assertRaises(RuntimeError):
            pcgw_worker(connection, "", "Game")
        connection.close.assert_called_once()
        connection.reset_mock()
        with mock.patch("game_manager.pcgamingwiki.PCGamingWiki", side_effect=RuntimeError("secret credentials")):
            pcgw_worker(connection, "", "Game")
        self.assertFalse(connection.send.call_args.args[0][0])
        self.assertNotIn("secret", connection.send.call_args.args[0][1])
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
