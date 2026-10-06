from __future__ import annotations

import os
import unittest
from unittest import mock

import requests

from game_manager.pcgamingwiki import PCGamingWiki, PCGamingWikiError, pcgw_worker


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

    def test_worker_success_failure_and_cleanup(self):
        connection = mock.Mock()
        client = mock.Mock()
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
