from __future__ import annotations

import unittest
from unittest import mock

import requests

from game_manager.network import NetworkError
from game_manager.steam import SteamError, lookup_steam_name, steam_name_worker


class SteamTests(unittest.TestCase):
    def response(self, app_id=570, name="Dota 2"):
        response = mock.Mock(status_code=200)
        response.json.return_value = {str(app_id): {"success": True, "data": {"steam_appid": app_id, "name": name}}}
        return response

    def lookup(self, response, app_id="570", proxy_url=""):
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch("game_manager.steam.create_session", return_value=session) as create_session:
            result = lookup_steam_name(app_id, proxy_url)
        create_session.assert_called_once_with(proxy_url)
        session.close.assert_called_once()
        response.close.assert_called_once()
        return result, session

    def test_lookup_uses_english_steam_store_and_closes_resources(self):
        name, session = self.lookup(self.response(name=" Dota 2 "))
        self.assertEqual(name, "Dota 2")
        args, kwargs = session.get.call_args
        self.assertEqual(args, ("https://store.steampowered.com/api/appdetails",))
        self.assertEqual(kwargs["params"], {"appids": "570", "l": "english"})
        self.assertEqual(kwargs["timeout"], (5, 15))

    def test_leading_zero_full_width_and_surrounding_spaces_are_normalized(self):
        for app_id in ("000570", "０００５７０", " 570 "):
            with self.subTest(app_id=app_id):
                name, session = self.lookup(self.response(), app_id)
                self.assertEqual(name, "Dota 2")
                self.assertEqual(session.get.call_args.kwargs["params"]["appids"], "570")

    def test_non_decimal_and_zero_values_never_open_network_session(self):
        for app_id in ("", " ", "0", "０００", "-570", "+570", "570.0", "Dota 2", "5 70", "²", None, 570):
            with self.subTest(app_id=app_id), mock.patch("game_manager.steam.create_session") as create_session:
                with self.assertRaisesRegex(SteamError, "有效的 Steam App ID"):
                    lookup_steam_name(app_id)
                create_session.assert_not_called()

    def test_proxy_and_direct_mode_use_shared_network_configuration(self):
        for proxy in ("", "http://127.0.0.1:7890", "socket://127.0.0.1:7890", "https://user:secret@127.0.0.1:7890"):
            with self.subTest(proxy=proxy):
                self.assertEqual(self.lookup(self.response(), proxy_url=proxy)[0], "Dota 2")

    def test_missing_application_is_reported_and_resources_are_closed(self):
        response = self.response()
        response.json.return_value = {"570": {"success": False}}
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch("game_manager.steam.create_session", return_value=session):
            with self.assertRaisesRegex(SteamError, "未找到"):
                lookup_steam_name("570")
        response.close.assert_called_once()
        session.close.assert_called_once()

    def test_invalid_response_and_application_mismatch_never_return_name(self):
        payloads = (None, [], {}, {"571": {"success": True}}, {"570": None},
                    {"570": {"success": "true", "data": {"steam_appid": 570, "name": "Dota 2"}}},
                    {"570": {"success": True, "data": None}},
                    {"570": {"success": True, "data": {"name": "Dota 2"}}},
                    {"570": {"success": True, "data": {"steam_appid": 571, "name": "Wrong Game"}}},
                    {"570": {"success": True, "data": {"steam_appid": "570", "name": "Dota 2"}}},
                    {"570": {"success": True, "data": {"steam_appid": True, "name": "Dota 2"}}},
                    {"570": {"success": True, "data": {"steam_appid": 570, "name": " "}}},
                    {"570": {"success": True, "data": {"steam_appid": 570, "name": None}}})
        for payload in payloads:
            response = self.response()
            response.json.return_value = payload
            session = mock.Mock()
            session.get.return_value = response
            with self.subTest(payload=payload), mock.patch("game_manager.steam.create_session", return_value=session):
                with self.assertRaisesRegex(SteamError, "无效数据"):
                    lookup_steam_name("570")
            response.close.assert_called_once()
            session.close.assert_called_once()

    def test_json_decode_failure_closes_response_and_session(self):
        response = self.response()
        response.json.side_effect = ValueError("secret")
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch("game_manager.steam.create_session", return_value=session):
            with self.assertRaisesRegex(SteamError, "无效数据") as caught:
                lookup_steam_name("570")
        self.assertNotIn("secret", str(caught.exception))
        response.close.assert_called_once()
        session.close.assert_called_once()

    def test_http_errors_close_resources_without_parsing_json(self):
        for status in (403, 429, 500):
            response = mock.Mock(status_code=status)
            session = mock.Mock()
            session.get.return_value = response
            with self.subTest(status=status), mock.patch("game_manager.steam.create_session", return_value=session):
                with self.assertRaisesRegex(SteamError, str(status)):
                    lookup_steam_name("570")
            response.json.assert_not_called()
            response.close.assert_called_once()
            session.close.assert_called_once()

    def test_connection_and_timeout_errors_hide_proxy_credentials_and_close_session(self):
        for error, message in ((requests.Timeout("http://user:secret@proxy:7890"), "超时"),
                               (requests.ConnectionError("http://user:secret@proxy:7890"), "无法连接"),
                               (requests.exceptions.ProxyError("http://user:secret@proxy:7890"), "无法连接")):
            session = mock.Mock()
            session.get.side_effect = error
            with self.subTest(error=error), mock.patch("game_manager.steam.create_session", return_value=session):
                with self.assertRaisesRegex(SteamError, message) as caught:
                    lookup_steam_name("570")
            self.assertNotIn("secret", str(caught.exception))
            session.close.assert_called_once()

    def test_invalid_proxy_uses_user_safe_network_error(self):
        with mock.patch("game_manager.steam.create_session", side_effect=NetworkError("代理地址无效")):
            with self.assertRaisesRegex(SteamError, "代理地址无效"):
                lookup_steam_name("570", "invalid")

    def test_worker_sends_success_and_closes_connection(self):
        connection = mock.Mock()
        with mock.patch("game_manager.steam.lookup_steam_name", return_value="Dota 2") as lookup:
            steam_name_worker(connection, "http://127.0.0.1:7890", "000570")
        lookup.assert_called_once_with("000570", "http://127.0.0.1:7890")
        connection.send.assert_called_once_with((True, "Dota 2"))
        connection.close.assert_called_once()

    def test_worker_sends_domain_error_and_hides_unexpected_error(self):
        for error, message in ((SteamError("Steam 未找到该编号"), "Steam 未找到该编号"),
                               (RuntimeError("secret"), "Steam 游戏名查询失败，请稍后重试或手动填写英文名。")):
            connection = mock.Mock()
            with self.subTest(error=error), mock.patch("game_manager.steam.lookup_steam_name", side_effect=error):
                steam_name_worker(connection, "", "570")
            connection.send.assert_called_once_with((False, message))
            connection.close.assert_called_once()

    def test_worker_closes_connection_when_result_receiver_is_gone(self):
        connection = mock.Mock()
        connection.send.side_effect = BrokenPipeError("receiver closed")
        with mock.patch("game_manager.steam.lookup_steam_name", return_value="Dota 2"):
            with self.assertRaises(BrokenPipeError):
                steam_name_worker(connection, "", "570")
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
