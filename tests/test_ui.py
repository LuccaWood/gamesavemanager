"""真实 Tk 集成测试；需要桌面会话，设置 GAME_MANAGER_GUI_TESTS=1 启用。"""
import gc
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from game_manager.storage import Storage


def held_artwork_worker(connection, api_key, proxy_url, action, value, destination=None):
    from PIL import Image
    folder = Path(destination)
    if action == "search":
        connection.send((True, [{"id": 42, "name": "Test Game"}]))
    else:
        folder.mkdir()
        Image.new("RGB", (1920, 620), "red").save(folder / "hero.png")
        manifest = {"game_id": value, "assets": {"hero": {"file": "hero.png"}}, "missing": []}
        (folder / "assets.json").write_text(json.dumps(manifest))
        (folder.parent / "started").touch()
        while not (folder.parent / "release").exists():
            time.sleep(0.02)
        connection.send((True, manifest))
    connection.close()


def empty_artwork_worker(connection, api_key, proxy_url, action, value, destination=None):
    connection.close()


def choice_artwork_worker(connection, api_key, proxy_url, action, value, destination=None):
    connection.send((True, [{"id": 42, "name": "First Match"}, {"id": 43, "name": "Second Match"}]))
    connection.close()


def save_location_worker(connection, proxy_url, english_name):
    connection.send((True, [{"title": english_name, "label": "Windows", "path": r"%LOCALAPPDATA%\TestGame\Saved",
                             "page_url": "https://www.pcgamingwiki.com/wiki/Test_Game", "resolved": True}]))
    connection.close()


def no_save_location_worker(connection, proxy_url, english_name):
    connection.send((True, []))
    connection.close()


def proxy_save_location_worker(connection, proxy_url, english_name):
    path = "%LOCALAPPDATA%\\" + ("Proxied" if proxy_url else "Direct")
    connection.send((True, [{"title": english_name, "label": "Windows", "path": path,
                             "page_url": "https://www.pcgamingwiki.com/wiki/Test_Game", "resolved": True}]))
    connection.close()


def local_save_location_worker(connection, proxy_url, english_name):
    connection.send((True, [{"title": english_name, "label": "Windows", "path": english_name.split("|", 1)[1],
                             "page_url": "https://www.pcgamingwiki.com/wiki/Test_Game", "resolved": True}]))
    connection.close()


def held_save_location_worker(connection, proxy_url, english_name):
    connection.send((True, [{"title": english_name, "label": "Windows", "path": r"%LOCALAPPDATA%\TestGame",
                             "page_url": "https://www.pcgamingwiki.com/wiki/Test_Game", "resolved": True}]))
    while True:
        time.sleep(0.02)


def choice_save_game_worker(connection, proxy_url, value):
    if isinstance(value, str):
        connection.send((True, {"games": [
            {"pageid": 101, "title": "Other Match", "page_url": "https://www.pcgamingwiki.com/wiki/Other_Match"},
            {"pageid": 202, "title": value, "page_url": "https://www.pcgamingwiki.com/wiki/Selected_Game"}]}))
    else:
        path = value["title"].split("|", 1)[1] if "|" in value["title"] else r"%LOCALAPPDATA%\SelectedGame"
        connection.send((True, [{"title": value["title"], "label": "Windows", "path": path,
                                 "page_url": value["page_url"], "resolved": True}]))
    connection.close()


def held_selected_save_game_worker(connection, proxy_url, value):
    if isinstance(value, str):
        choice_save_game_worker(connection, proxy_url, value)
    else:
        held_save_location_worker(connection, proxy_url, value["title"])


def choice_steam_cloud_worker(connection, proxy_url, value):
    if isinstance(value, str):
        connection.send((True, {"source": "Steam 云存档", "games": [
            {"id": 101, "name": "Other Steam Game", "source": "Steam 云存档"},
            {"id": 202, "name": value, "source": "Steam 云存档"}]}))
    else:
        path = value["name"].split("|", 1)[1] if "|" in value["name"] else r"%LOCALAPPDATA%\SteamGame"
        connection.send((True, [{"title": value["name"], "label": "Steam 云同步规则 · *.sav",
                                 "source": "Steam 云存档", "path": path, "resolved": True,
                                 "page_url": f"https://steamdb.info/app/{value['id']}/ufs/"}]))
    connection.close()


def held_selected_steam_cloud_worker(connection, proxy_url, value):
    if isinstance(value, str):
        choice_steam_cloud_worker(connection, proxy_url, value)
    else:
        while True:
            time.sleep(0.02)


def gallery_artwork_worker(connection, api_key, proxy_url, action, value, destination=None):
    from PIL import Image
    sizes = {"cover": (600, 900), "wide": (920, 430), "hero": (1920, 620), "logo": (300, 120)}
    if action == "search":
        result = [{"id": 42, "name": "Test Game"}]
    else:
        folder = Path(destination)
        folder.mkdir()
        kind = value["kind"]
        if action == "gallery":
            page = value["page"]
            candidates = []
            for index in range(2):
                asset_id = 100 + page * 2 + index
                filename = f"preview-{asset_id}.png"
                Image.new("RGB", (120, 120), "red" if index == 0 else "blue").save(folder / filename)
                candidates.append({"id": asset_id, "url": f"https://cdn2.steamgriddb.com/grid/{asset_id}.png",
                                   "width": sizes[kind][0], "height": sizes[kind][1], "preview_file": filename,
                                   "author": {"name": f"Author {asset_id}"}})
            result = {"game_id": value["game_id"], "kind": kind, "page": page, "has_next": page == 0,
                      "candidates": candidates}
        else:
            filename = kind + (".png" if kind == "logo" else ".jpg")
            Image.new("RGB", sizes[kind], "blue").save(folder / filename)
            result = {"game_id": value["game_id"], "assets": {kind: {"file": filename, "id": value["candidate"]["id"]}}, "missing": []}
            (folder / "assets.json").write_text(json.dumps(result))
        if api_key == "held":
            (folder.parent / "started").touch()
            while not (folder.parent / "release").exists():
                time.sleep(0.02)
    connection.send((True, result))
    connection.close()


def choice_gallery_artwork_worker(connection, api_key, proxy_url, action, value, destination=None):
    if action == "search":
        choice_artwork_worker(connection, api_key, proxy_url, action, value, destination)
    else:
        gallery_artwork_worker(connection, api_key, proxy_url, action, value, destination)


def steam_name_worker(connection, proxy_url, app_id):
    connection.send((True, {570: "Dota 2", 123: "Other Game", 2048: "2048"}[int(app_id)]))
    connection.close()


def failed_steam_name_worker(connection, proxy_url, app_id):
    connection.send((False, "Steam 未找到该编号，请检查编号。"))
    connection.close()


def held_steam_name_worker(connection, proxy_url, app_id):
    connection.send((True, "Dota 2"))
    while True:
        time.sleep(0.02)


def empty_steam_name_worker(connection, proxy_url, app_id):
    connection.close()


@unittest.skipUnless(os.environ.get("GAME_MANAGER_GUI_TESTS") == "1", "需要可用桌面会话")
class UITests(unittest.TestCase):
    def setUp(self):
        from game_manager.ui import GameManagerApp
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.save_dir = self.root / "中文存档 空格"
        self.save_dir.mkdir()
        (self.save_dir / "slot.dat").write_text("original", encoding="utf-8")
        storage = Storage(self.root / "data")
        self.game = storage.save_game({"english_name": "Test Game", "chinese_name": "测试游戏",
                                       "save_path": str(self.save_dir)})
        self.patches = [patch("game_manager.ui.messagebox.askyesno", return_value=True),
                        patch("game_manager.ui.messagebox.showinfo"),
                        patch("game_manager.ui.messagebox.showerror"),
                        patch("game_manager.ui.pcgw_worker", no_save_location_worker)]
        for item in self.patches:
            item.start()
        self.app = GameManagerApp(self.root / "data")
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        from customtkinter import AppearanceModeTracker
        if getattr(self.app, "artwork_job", None):
            self.app.stop_artwork()
            self.wait_for_artwork()
        self.app.destroy()
        AppearanceModeTracker.app_list.remove(self.app)
        self.app = None
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()
        gc.collect()

    def wait_for_task(self):
        deadline = time.monotonic() + 8
        while self.app.busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy, "后台任务未能按时完成")
        self.app.update_idletasks()

    def wait_until(self, condition):
        deadline = time.monotonic() + 8
        while not condition() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertTrue(condition(), "图片任务未能按时达到预期状态")

    def wait_for_artwork(self):
        self.wait_until(lambda: self.app.artwork_job is None)

    def start_held_download(self):
        self.app.api_key = "test-key"
        self.app.search_artwork()
        self.wait_until(lambda: self.app.artwork_job is not None
                        and self.app.artwork_job["action"] == "download"
                        and (self.app.artwork_job["temporary"] / "started").exists())
        return self.app.artwork_job

    def choose_pcgw_article(self, page_id):
        from customtkinter import CTkButton

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        buttons = [widget for widget in descendants(self.app.dialog) if isinstance(widget, CTkButton)
                   and widget.cget("text").endswith(f"ID {page_id}")]
        self.assertEqual(len(buttons), 1)
        buttons[0].invoke()

    def seed_artwork(self):
        from PIL import Image
        folder = self.app.storage.artwork_dir(self.game)
        folder.mkdir(parents=True)
        for kind, size, color in (("cover", (600, 900), "red"), ("hero", (1920, 620), "green")):
            Image.new("RGB", size, color).save(folder / f"{kind}.png")
        manifest = {"game_id": 42, "notes": "keep", "assets": {
            "cover": {"file": "cover.png"}, "hero": {"file": "hero.png"}}, "missing": ["wide", "logo"]}
        (folder / "assets.json").write_text(json.dumps(manifest))
        return folder, {path.name: path.read_bytes() for path in folder.iterdir()}

    def add_archived_game(self, name):
        game = self.app.storage.save_game({"english_name": name, "save_path": str(self.save_dir)})
        self.app.backups.create(game)
        return game

    def test_backup_copy_restore_export_and_delete_through_ui(self):
        self.app.create_backup()
        self.wait_for_task()
        records = self.app.backups.list_backups(self.game)
        self.assertEqual(len(records), 1)
        original_id = records[0]["id"]
        self.assertIn(original_id, self.app.backup_table.get_children())
        self.app.backup_table.selection_set(original_id)
        self.app.copy_backup()
        self.wait_for_task()
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 2)
        (self.save_dir / "slot.dat").write_text("changed", encoding="utf-8")
        (self.save_dir / "extra.dat").write_text("remove", encoding="utf-8")
        self.app.backup_table.selection_set(original_id)
        self.app.restore_backup()
        self.wait_for_task()
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")
        self.assertFalse((self.save_dir / "extra.dat").exists())
        self.assertEqual(len(self.app.backup_table.get_children()), 2)
        self.app.tabs.set("数据打包")
        self.app.export_game()
        self.wait_for_task()
        self.assertEqual(self.app.tabs.get(), "数据打包")
        export = self.app.export_paths[self.game["id"]]
        self.assertTrue(export.is_file())
        with zipfile.ZipFile(export) as archive:
            self.assertIn("game.json", archive.namelist())
            self.assertEqual(sum(name.endswith("save.zip") for name in archive.namelist()), 2)
        self.app.backup_table.selection_set(original_id)
        self.app.delete_backup()
        self.wait_for_task()
        self.assertEqual(len(self.app.backup_table.get_children()), 1)

    def test_delete_game_removes_backups_artwork_and_keeps_other_data(self):
        from game_manager.ui import messagebox
        body = self.root / "game-body"
        body.mkdir()
        (body / "game.exe").write_bytes(b"game")
        self.game = self.app.storage.save_game({**self.game, "game_path": str(body)})
        self.app.backups.create(self.game)
        self.seed_artwork()
        exported = self.app.backups.export(self.game)
        export_content = exported.read_bytes()
        second = self.app.storage.save_game({"english_name": "Second Game", "save_path": str(self.save_dir)})
        self.app.backups.create(second)
        other = self.app.storage.game_dir(second)
        original = {path.relative_to(other): path.read_bytes() for path in other.rglob("*") if path.is_file()}
        directory = self.app.storage.game_dir(self.game)
        self.app.refresh()
        self.app.delete_game()
        self.wait_for_task()
        with self.assertRaises(ValueError):
            self.app.storage.get_game(self.game["id"])
        self.assertFalse(directory.exists())
        self.assertEqual(self.app.selected_id, second["id"])
        self.assertEqual({path.relative_to(other): path.read_bytes() for path in other.rglob("*") if path.is_file()}, original)
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")
        self.assertEqual((body / "game.exe").read_bytes(), b"game")
        self.assertEqual(exported.read_bytes(), export_content)
        self.assertIn("全部备份和图片", messagebox.askyesno.call_args.args[1])

    def test_game_checkboxes_are_hidden_until_delete_mode_is_requested(self):
        from customtkinter import CTkCheckBox
        second = self.add_archived_game("Second Game")
        self.app.refresh()
        self.app.select(second["id"])
        self.assertEqual(self.app.game_checks, {})
        self.assertFalse(any(isinstance(widget, CTkCheckBox) for row in self.app.game_list.winfo_children()
                             for widget in row.winfo_children()))
        self.assertEqual(self.app.delete_games_button.cget("text"), "删除勾选游戏")
        self.assertEqual(self.app.delete_games_button.cget("state"), "normal")
        self.assertEqual(self.app.cancel_game_selection_button.grid_info(), {})
        with patch("game_manager.ui.messagebox.askyesno") as confirm:
            self.app.delete_games_button.invoke()
            confirm.assert_not_called()
        self.assertTrue(self.app.game_selection_mode)
        self.assertEqual(set(self.app.game_checks), {self.game["id"], second["id"]})
        self.assertTrue(all(check.get() == 0 for check in self.app.game_checks.values()))
        self.assertIn("（0）", self.app.delete_games_button.cget("text"))
        self.assertEqual(self.app.delete_games_button.cget("state"), "disabled")
        self.assertEqual(self.app.cancel_game_selection_button.winfo_manager(), "grid")
        self.assertEqual(self.app.selected_id, second["id"])

    def test_cancel_game_selection_clears_checks_without_deleting_games(self):
        self.app.backups.create(self.game)
        second = self.add_archived_game("Second Game")
        self.app.refresh()
        self.app.delete_games_button.invoke()
        for game in (self.game, second):
            self.app.game_checks[game["id"]].toggle()
        self.app.search.insert(0, "Second")
        self.app.select(second["id"])
        with patch("game_manager.ui.messagebox.askyesno") as confirm:
            self.app.cancel_game_selection_button.invoke()
            confirm.assert_not_called()
        self.assertFalse(self.app.game_selection_mode)
        self.assertEqual(self.app.game_checks, {})
        self.assertEqual(self.app.checked_game_ids, set())
        self.assertEqual(self.app.cancel_game_selection_button.grid_info(), {})
        self.assertEqual(self.app.delete_games_button.cget("state"), "normal")
        self.assertEqual(self.app.selected_id, second["id"])
        for game in (self.game, second):
            self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.app.delete_games_button.invoke()
        self.app.search.delete(0, "end")
        self.app.refresh_sidebar()
        self.assertTrue(all(check.get() == 0 for check in self.app.game_checks.values()))

    def test_deleting_current_game_keeps_other_checked_games_in_selection_mode(self):
        second = self.add_archived_game("Second Game")
        self.app.refresh()
        self.app.delete_games_button.invoke()
        self.app.game_checks[second["id"]].toggle()
        self.app.delete_game()
        self.wait_for_task()
        self.assertEqual(self.app.storage.games, [second])
        self.assertTrue(self.app.game_selection_mode)
        self.assertEqual(self.app.checked_game_ids, {second["id"]})
        self.assertEqual(self.app.game_checks[second["id"]].get(), 1)
        self.assertEqual(self.app.delete_games_button.cget("state"), "normal")

    def test_multiple_checked_games_delete_their_archives(self):
        from game_manager.ui import messagebox
        self.app.backups.create(self.game)
        self.seed_artwork()
        exported = self.app.backups.export(self.game)
        second = self.app.storage.save_game({"english_name": "Second Game", "save_path": str(self.save_dir)})
        third = self.app.storage.save_game({"english_name": "Third Game", "save_path": str(self.save_dir)})
        for game in (second, third):
            self.app.backups.create(game)
        self.app.refresh()
        self.app.delete_games_button.invoke()
        self.app.game_checks[self.game["id"]].toggle()
        self.app.game_checks[second["id"]].toggle()
        self.assertEqual(self.app.checked_game_ids, {self.game["id"], second["id"]})
        with patch.object(self.app, "report_callback_exception") as callback_error:
            self.app.delete_games_button.invoke()
            self.wait_for_task()
            callback_error.assert_not_called()
        self.assertEqual(self.app.storage.games, [third])
        self.assertFalse(self.app.storage.game_dir(self.game).exists())
        self.assertFalse(self.app.storage.game_dir(second).exists())
        self.assertEqual(len(self.app.backups.list_backups(third)), 1)
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")
        self.assertTrue(exported.is_file())
        self.assertEqual(self.app.selected_id, third["id"])
        self.assertEqual(self.app.checked_game_ids, set())
        self.assertFalse(self.app.game_selection_mode)
        self.assertEqual(self.app.game_checks, {})
        self.assertEqual(self.app.delete_games_button.cget("state"), "normal")
        self.assertEqual(self.app.cancel_game_selection_button.grid_info(), {})
        messagebox.askyesno.assert_called_once()
        prompt = messagebox.askyesno.call_args.args[1]
        self.assertIn("Test Game", prompt)
        self.assertIn("Second Game", prompt)
        self.assertNotIn("Third Game", prompt)

    def test_selected_game_is_empty_while_its_batch_deletion_is_pending(self):
        second = self.app.storage.save_game({"english_name": "Second Game", "save_path": str(self.save_dir)})
        self.app.delete_games_button.invoke()
        self.app.checked_game_ids = {self.game["id"], second["id"]}
        entered, release = threading.Event(), threading.Event()
        delete = self.app.delete_game_data

        def hold_after_delete(game):
            result = delete(game)
            if game["id"] == self.game["id"]:
                entered.set()
                release.wait(timeout=3)
            return result

        with patch.object(self.app, "delete_game_data", side_effect=hold_after_delete):
            self.app.delete_checked_games()
            try:
                self.assertTrue(entered.wait(timeout=3))
                self.assertTrue(self.app.busy)
                self.assertIsNone(self.app.selected_game())
            finally:
                release.set()
                self.wait_for_task()

    def test_game_checks_survive_filtering_details_switch_and_cancel(self):
        from game_manager.ui import messagebox
        self.app.backups.create(self.game)
        second = self.add_archived_game("Second Game")
        third = self.add_archived_game("Third Game")
        self.app.refresh()
        self.app.delete_games_button.invoke()
        for game in (self.game, second):
            self.app.game_checks[game["id"]].toggle()
        self.app.search.insert(0, "Third")
        self.app.refresh_sidebar()
        self.app.select(third["id"])
        self.assertEqual(set(self.app.game_checks), {third["id"]})
        self.assertEqual(self.app.checked_game_ids, {self.game["id"], second["id"]})
        self.assertIn("（2）", self.app.delete_games_button.cget("text"))
        with patch("game_manager.ui.messagebox.askyesno", return_value=False) as confirm:
            self.app.delete_checked_games()
        self.assertEqual(len(self.app.storage.games), 3)
        for game in (self.game, second, third):
            self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.assertEqual(self.app.checked_game_ids, {self.game["id"], second["id"]})
        self.assertTrue(self.app.game_selection_mode)
        prompt = confirm.call_args.args[1]
        self.assertIn("Test Game", prompt)
        self.assertIn("Second Game", prompt)
        self.assertNotIn("Third Game", prompt)
        self.app.search.delete(0, "end")
        self.app.refresh_sidebar()
        self.assertEqual(self.app.game_checks[self.game["id"]].get(), 1)
        self.assertEqual(self.app.game_checks[second["id"]].get(), 1)
        self.app.game_checks[self.game["id"]].toggle()
        self.assertEqual(self.app.checked_game_ids, {second["id"]})

    def test_batch_game_delete_validates_all_directories_before_deleting_any(self):
        from game_manager.ui import messagebox
        self.app.backups.create(self.game)
        second = self.app.storage.save_game({"english_name": "Second Game", "save_path": str(self.save_dir)})
        third = self.add_archived_game("Third Game")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.dat").write_bytes(b"keep")
        link = self.app.storage.game_dir(second)
        link.symlink_to(outside, target_is_directory=True)
        self.app.delete_games_button.invoke()
        self.app.checked_game_ids = {game["id"] for game in self.app.storage.games}
        self.app.refresh()
        try:
            self.app.delete_checked_games()
            self.wait_for_task()
            self.assertEqual(len(self.app.storage.games), 3)
            self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)
            self.assertEqual(len(self.app.backups.list_backups(third)), 1)
            self.assertEqual((outside / "keep.dat").read_bytes(), b"keep")
            self.assertIn("已删除 0 / 共 3", messagebox.showerror.call_args.args[1])
            self.assertIn("Second Game", messagebox.showerror.call_args.args[1])
            self.assertTrue(self.app.game_selection_mode)
        finally:
            link.unlink()

    def test_batch_game_delete_configuration_failure_stops_after_first_success(self):
        from game_manager.ui import messagebox
        self.app.backups.create(self.game)
        second = self.add_archived_game("Second Game")
        third = self.add_archived_game("Third Game")
        save = self.app.storage._save

        def fail_second(games, settings):
            if not any(game["id"] == second["id"] for game in games):
                raise OSError("second configuration failed")
            return save(games, settings)

        self.app.delete_games_button.invoke()
        self.app.checked_game_ids = {game["id"] for game in self.app.storage.games}
        self.app.refresh()
        with patch.object(self.app.storage, "_save", side_effect=fail_second):
            self.app.delete_checked_games()
            self.wait_for_task()
        self.assertEqual(self.app.storage.games, [second, third])
        self.assertFalse(self.app.storage.game_dir(self.game).exists())
        for game in (second, third):
            self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.assertEqual(self.app.checked_game_ids, {second["id"], third["id"]})
        self.assertTrue(self.app.game_selection_mode)
        self.assertEqual(self.app.selected_id, second["id"])
        self.assertIn("已删除 1 / 共 3", messagebox.showerror.call_args.args[1])
        self.assertIn("Second Game", messagebox.showerror.call_args.args[1])

    def test_batch_game_delete_cleanup_failure_does_not_restore_previously_deleted_game(self):
        from game_manager.ui import messagebox, shutil
        self.app.backups.create(self.game)
        second = self.add_archived_game("Second Game")
        third = self.add_archived_game("Third Game")
        artwork = self.app.storage.artwork_dir(second)
        artwork.mkdir()
        (artwork / "logo.png").write_bytes(b"image")
        remove = shutil.rmtree
        calls = []

        def fail_second(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                (path / "artwork" / "logo.png").unlink()
                raise PermissionError("second image locked")
            return remove(path, *args, **kwargs)

        self.app.delete_games_button.invoke()
        self.app.checked_game_ids = {game["id"] for game in self.app.storage.games}
        self.app.refresh()
        with patch("game_manager.ui.shutil.rmtree", side_effect=fail_second):
            self.app.delete_checked_games()
            self.wait_for_task()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.app.storage.games, [second, third])
        self.assertFalse(self.app.storage.game_dir(self.game).exists())
        self.assertFalse((artwork / "logo.png").exists())
        for game in (second, third):
            self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.assertEqual(self.app.checked_game_ids, {second["id"], third["id"]})
        self.assertTrue(self.app.game_selection_mode)
        self.assertEqual(self.app.selected_id, second["id"])
        error = messagebox.showerror.call_args.args[1]
        self.assertIn("已删除 1 / 共 3", error)
        self.assertIn("部分文件可能已删除", error)
        self.assertIn(str(artwork.parent), error)

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_batch_game_delete_stops_artwork_for_any_checked_game(self):
        second = self.add_archived_game("Second Game")
        self.app.start_artwork_job("download", second, {"api_key": "test-key", "proxy_url": ""}, 42)
        job = self.app.artwork_job
        self.wait_until(lambda: (job["temporary"] / "started").exists())
        (job["temporary"] / "release").touch()
        job["process"].join(timeout=3)
        self.app.delete_games_button.invoke()
        self.app.checked_game_ids = {self.game["id"], second["id"]}
        self.app.delete_checked_games()
        self.assertTrue(job["cancelled"])
        deadline = time.monotonic() + 3
        while self.app.results.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.app.results.empty())
        self.wait_for_task()
        self.wait_for_artwork()
        self.assertEqual(self.app.storage.games, [])
        self.assertFalse(self.app.storage.game_dir(second).exists())
        self.assertFalse(job["temporary"].exists())
        self.assertIn("已删除", self.app.status.cget("text"))
        self.assertNotIn("原有图片已保留", self.app.status.cget("text"))

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_batch_game_delete_keeps_other_game_artwork_task_running(self):
        second = self.add_archived_game("Second Game")
        third = self.add_archived_game("Third Game")
        self.app.start_artwork_job("download", second, {"api_key": "test-key", "proxy_url": ""}, 42)
        job = self.app.artwork_job
        self.wait_until(lambda: (job["temporary"] / "started").exists())
        self.app.delete_games_button.invoke()
        self.app.cancel_game_selection_button.invoke()
        self.assertIs(self.app.artwork_job, job)
        self.assertFalse(job["cancelled"])
        self.app.delete_games_button.invoke()
        self.app.checked_game_ids = {self.game["id"], third["id"]}
        self.app.delete_checked_games()
        self.wait_for_task()
        self.assertIs(self.app.artwork_job, job)
        self.assertFalse(job["cancelled"])
        self.assertEqual(self.app.storage.games, [second])
        (job["temporary"] / "release").touch()
        self.wait_for_artwork()
        self.assertTrue((self.app.storage.artwork_dir(second) / "hero.png").is_file())
        self.assertEqual(len(self.app.backups.list_backups(second)), 1)

    def test_batch_game_controls_empty_busy_and_minimum_window_layout(self):
        with patch("game_manager.ui.messagebox.askyesno") as confirm:
            self.app.delete_games_button.invoke()
            confirm.assert_not_called()
        self.assertEqual(self.app.delete_games_button.cget("state"), "disabled")
        for index in range(12):
            self.app.storage.save_game({"english_name": f"Game {index}", "save_path": str(self.save_dir)})
        self.app.refresh()
        self.app.deiconify()
        self.app.geometry("1020x720")
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        button = self.app.delete_games_button
        self.assertTrue(button.winfo_ismapped())
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), self.app.sidebar.winfo_rooty() + self.app.sidebar.winfo_height())
        cancel = self.app.cancel_game_selection_button
        self.assertTrue(cancel.winfo_ismapped())
        self.assertGreaterEqual(cancel.winfo_rootx(), button.winfo_rootx() + button.winfo_width())
        self.assertLessEqual(cancel.winfo_rootx() + cancel.winfo_width(), self.app.sidebar.winfo_rootx() + self.app.sidebar.winfo_width())
        self.app.game_list._parent_canvas.yview_moveto(1)
        self.app.update()
        last = self.app.game_checks[self.app.storage.games[-1]["id"]]
        canvas = self.app.game_list._parent_canvas
        self.assertGreaterEqual(last.winfo_rooty(), canvas.winfo_rooty())
        self.assertLessEqual(last.winfo_rooty() + last.winfo_height(), canvas.winfo_rooty() + canvas.winfo_height())
        self.app.checked_game_ids = {game["id"] for game in self.app.storage.games}
        self.app.refresh()
        self.app.run_task("等待任务", lambda: time.sleep(0.15), lambda _: None)
        self.assertEqual(self.app.delete_games_button.cget("state"), "disabled")
        self.assertEqual(cancel.cget("state"), "disabled")
        cancel.invoke()
        self.assertTrue(self.app.game_selection_mode)
        self.assertTrue(all(check.cget("state") == "disabled" for check in self.app.game_checks.values()))
        self.app.game_checks[self.game["id"]].toggle()
        self.assertEqual(len(self.app.checked_game_ids), 13)
        with patch("game_manager.ui.messagebox.askyesno") as confirm:
            self.app.delete_checked_games()
            confirm.assert_not_called()
        self.wait_for_task()
        self.app.delete_games_button.invoke()
        self.wait_for_task()
        self.assertEqual(self.app.storage.games, [])
        self.assertEqual(self.app.checked_game_ids, set())
        self.assertEqual(self.app.game_checks, {})
        self.assertFalse(self.app.game_selection_mode)
        self.assertEqual(self.app.delete_games_button.cget("state"), "disabled")
        self.assertEqual(cancel.grid_info(), {})

    def test_cancelled_game_deletion_keeps_record_and_all_archives(self):
        self.app.backups.create(self.game)
        self.seed_artwork()
        directory = self.app.storage.game_dir(self.game)
        original = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        with patch("game_manager.ui.messagebox.askyesno", return_value=False):
            self.app.delete_game()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.storage.get_game(self.game["id"]), self.game)
        self.assertEqual({path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}, original)

    def test_deleting_game_without_archives_leaves_empty_library(self):
        directory = self.app.storage.game_dir(self.game)
        self.assertFalse(directory.exists())
        self.app.delete_game()
        self.wait_for_task()
        self.assertEqual(self.app.storage.games, [])
        self.assertIsNone(self.app.selected_id)
        self.assertFalse(directory.exists())
        self.assertTrue(self.app.winfo_exists())

    def test_game_delete_configuration_failure_restores_archives(self):
        from game_manager.ui import messagebox
        self.app.backups.create(self.game)
        self.seed_artwork()
        directory = self.app.storage.game_dir(self.game)
        original = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        library = (self.app.storage.data_dir / "library.json").read_bytes()
        with patch.object(self.app.storage, "_save", side_effect=OSError("disk full")):
            self.app.delete_game()
            self.wait_for_task()
        self.assertEqual((self.app.storage.data_dir / "library.json").read_bytes(), library)
        self.assertEqual({path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}, original)
        self.assertEqual(list(directory.parent.glob(".deleting-*")), [])
        self.assertIn("disk full", messagebox.showerror.call_args.args[1])

    def test_game_delete_directory_move_failure_keeps_record_and_files(self):
        from game_manager.ui import messagebox
        folder, original = self.seed_artwork()
        with patch.object(Path, "rename", side_effect=PermissionError("locked directory")):
            self.app.delete_game()
            self.wait_for_task()
        self.assertEqual(self.app.storage.get_game(self.game["id"]), self.game)
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        self.assertIn("locked directory", messagebox.showerror.call_args.args[1])

    def test_game_delete_partial_cleanup_failure_keeps_record_and_reports_remaining_files(self):
        from game_manager.ui import messagebox
        self.app.backups.create(self.game)
        folder, original = self.seed_artwork()
        library = (self.app.storage.data_dir / "library.json").read_bytes()

        def partial_delete(path):
            (path / "artwork" / "cover.png").unlink()
            raise PermissionError("locked image")

        with patch("game_manager.ui.shutil.rmtree", side_effect=partial_delete):
            self.app.delete_game()
            self.wait_for_task()
        self.assertEqual((self.app.storage.data_dir / "library.json").read_bytes(), library)
        self.assertEqual(self.app.selected_id, self.game["id"])
        self.assertFalse((folder / "cover.png").exists())
        self.assertEqual((folder / "hero.png").read_bytes(), original["hero.png"])
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)
        error = messagebox.showerror.call_args.args[1]
        self.assertIn("部分文件可能已删除", error)
        self.assertIn(str(self.app.storage.game_dir(self.game)), error)
        self.assertNotIn("已删除 Test Game", self.app.status.cget("text"))
        self.assertTrue(self.app.winfo_exists())

    def test_game_delete_metadata_rollback_failure_keeps_ui_running_and_reports_path(self):
        from game_manager.ui import messagebox
        folder, original = self.seed_artwork()
        save = self.app.storage._save

        def fail_restore(games, settings):
            if any(game["id"] == self.game["id"] for game in games):
                raise OSError("restore failed")
            return save(games, settings)

        with patch.object(self.app.storage, "_save", side_effect=fail_restore), \
                patch("game_manager.ui.shutil.rmtree", side_effect=PermissionError("locked image")):
            self.app.delete_game()
            self.wait_for_task()
        self.assertEqual(self.app.storage.games, [])
        self.assertIsNone(self.app.selected_id)
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        self.assertIn("游戏条目恢复失败", messagebox.showerror.call_args.args[1])
        self.assertIn(str(folder.parent), messagebox.showerror.call_args.args[1])
        self.assertTrue(self.app.winfo_exists())

    def test_game_delete_directory_rollback_failure_reports_preserved_archive_path(self):
        from game_manager.ui import messagebox
        self.seed_artwork()
        directory = self.app.storage.game_dir(self.game)
        rename = Path.rename

        def fail_restore(path, destination):
            if path.name.startswith(".deleting-"):
                raise PermissionError("restore directory locked")
            return rename(path, destination)

        with patch.object(Path, "rename", fail_restore), \
                patch("game_manager.ui.shutil.rmtree", side_effect=PermissionError("locked image")):
            self.app.delete_game()
            self.wait_for_task()
        preserved = list(directory.parent.glob(".deleting-*"))
        self.assertEqual(len(preserved), 1)
        self.assertTrue((preserved[0] / "artwork" / "hero.png").is_file())
        self.assertEqual(self.app.storage.get_game(self.game["id"]), self.game)
        self.assertIn(str(preserved[0]), messagebox.showerror.call_args.args[1])
        self.assertIn("目录恢复失败", messagebox.showerror.call_args.args[1])
        self.app.add_game()
        self.assertTrue(self.app.dialog.winfo_exists())

    def test_game_delete_rejects_linked_game_directory_and_parent(self):
        from game_manager.ui import messagebox
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.dat").write_bytes(b"keep")
        directory = self.app.storage.game_dir(self.game)
        directory.parent.mkdir(parents=True)
        for link in (directory, directory.parent):
            with self.subTest(link=link):
                if link == directory.parent:
                    link.rmdir()
                link.symlink_to(outside, target_is_directory=True)
                try:
                    self.app.delete_game()
                    self.wait_for_task()
                    self.assertEqual(self.app.storage.get_game(self.game["id"]), self.game)
                    self.assertEqual((outside / "keep.dat").read_bytes(), b"keep")
                    self.assertIn("不能是链接", messagebox.showerror.call_args.args[1])
                finally:
                    link.unlink()

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_deleting_game_stops_completed_artwork_result_without_recreating_directory(self):
        self.seed_artwork()
        job = self.start_held_download()
        (job["temporary"] / "release").touch()
        job["process"].join(timeout=3)
        self.assertFalse(job["process"].is_alive())
        self.app.delete_game()
        self.assertTrue(job["cancelled"])
        self.wait_for_task()
        self.wait_for_artwork()
        self.assertFalse(self.app.storage.game_dir(self.game).exists())
        self.assertFalse(job["temporary"].exists())
        self.assertEqual(self.app.storage.games, [])

    def test_game_delete_is_ignored_while_busy_or_closing(self):
        self.app.run_task("等待任务", lambda: time.sleep(0.15), lambda _: None)
        with patch("game_manager.ui.messagebox.askyesno") as confirm:
            self.app.delete_game()
            confirm.assert_not_called()
        self.wait_for_task()
        with patch.object(self.app, "_closing", True), patch("game_manager.ui.messagebox.askyesno") as confirm:
            self.app.delete_game()
            confirm.assert_not_called()
        self.assertEqual(self.app.storage.get_game(self.game["id"]), self.game)

    def test_delete_multiple_selected_backups_with_one_confirmation(self):
        from game_manager.ui import messagebox
        records = [self.app.backups.create(self.game) for _ in range(3)]
        self.app.refresh()
        self.assertEqual(str(self.app.backup_table.cget("selectmode")), "extended")
        self.app.backup_table.selection_set([records[0]["id"], records[2]["id"]])
        self.app.delete_backup()
        self.wait_for_task()
        messagebox.askyesno.assert_called_once()
        self.assertIn("2 条", messagebox.askyesno.call_args.args[1])
        self.assertEqual(self.app.backup_table.get_children(), (records[1]["id"],))
        self.assertEqual([entry["id"] for entry in self.app.backups.list_backups(self.game)], [records[1]["id"]])
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")

    def test_cancel_multiple_backup_deletion_preserves_all_records(self):
        records = [self.app.backups.create(self.game) for _ in range(2)]
        self.app.refresh()
        self.app.backup_table.selection_set([entry["id"] for entry in records])
        with patch("game_manager.ui.messagebox.askyesno", return_value=False):
            self.app.delete_backup()
        self.assertFalse(self.app.busy)
        self.assertEqual({entry["id"] for entry in self.app.backups.list_backups(self.game)}, {entry["id"] for entry in records})

    def test_restore_copy_and_open_require_one_selected_backup(self):
        from game_manager.ui import messagebox
        records = [self.app.backups.create(self.game) for _ in range(2)]
        self.app.refresh()
        self.app.backup_table.selection_set([entry["id"] for entry in records])
        with patch.object(self.app, "run_task") as run, patch("game_manager.ui.open_folder") as open_folder:
            self.app.restore_backup()
            self.app.copy_backup()
            self.app.open_backup()
        run.assert_not_called()
        open_folder.assert_not_called()
        messagebox.askyesno.assert_not_called()
        self.assertEqual(messagebox.showinfo.call_count, 3)

    def test_partial_multiple_deletion_failure_refreshes_remaining_records(self):
        import shutil
        from game_manager.ui import messagebox
        records = [self.app.backups.create(self.game) for _ in range(4)]
        self.app.refresh()
        self.app.backup_table.selection_set([records[index]["id"] for index in (0, 2, 3)])
        selected = self.app.backup_table.selection()
        failed = self.app.backups.backup_dir(self.game, selected[1])
        original_rmtree = shutil.rmtree

        def remove(path, *args, **kwargs):
            if Path(path) == failed:
                raise PermissionError("simulated locked backup")
            return original_rmtree(path, *args, **kwargs)

        with patch("game_manager.backups.shutil.rmtree", side_effect=remove):
            self.app.delete_backup()
            self.wait_for_task()
        expected = {entry["id"] for entry in records} - {selected[0]}
        self.assertEqual(set(self.app.backup_table.get_children()), expected)
        self.assertEqual({entry["id"] for entry in self.app.backups.list_backups(self.game)}, expected)
        self.assertIn("已删除 1 / 共 3", messagebox.showerror.call_args.args[1])
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")

    def test_busy_disables_switching_and_controls(self):
        second = self.app.storage.save_game({"english_name": "Second Game"})
        self.app.refresh()
        self.app.run_task("等待测试任务", lambda: time.sleep(0.15), lambda _: None)
        self.app.select(second["id"])
        self.assertEqual(self.app.selected_id, self.game["id"])
        self.assertEqual(self.app.add_button.cget("state"), "disabled")
        self.assertEqual(self.app.import_button.cget("state"), "disabled")
        self.assertEqual(self.app.library_button.cget("state"), "disabled")
        self.assertTrue(all(button.cget("state") == "disabled" for button in self.app.game_buttons))
        self.wait_for_task()
        self.assertEqual(self.app.add_button.cget("state"), "normal")

    def test_corrupt_backup_does_not_prevent_ui_from_opening(self):
        record = self.app.backups.create(self.game)
        metadata = self.app.backups.backup_dir(self.game, record["id"]) / "metadata.json"
        metadata.write_text("{broken", encoding="utf-8")
        self.app.refresh()
        self.app.update_idletasks()
        self.assertEqual(self.app.backup_table.get_children(), ())
        self.assertEqual(metadata.read_text(), "{broken")
        self.assertTrue(self.app.winfo_exists())
        artwork = self.app.storage.artwork_dir(self.game)
        artwork.mkdir(parents=True)
        for value in ([], {"assets": []}, {"assets": {"cover": {"file": "../outside.png"}}}):
            (artwork / "assets.json").write_text(json.dumps(value), encoding="utf-8")
            self.app.refresh()
            self.assertIn("warning", self.app.artwork_manifest(self.game))

    def test_backup_note_edit_persists_and_shows_in_centered_column(self):
        backup = self.app.backups.create(self.game)
        self.app.refresh()
        self.assertIn("note", self.app.backup_table.cget("columns"))
        self.assertEqual(str(self.app.backup_table.column("note", "anchor")), "center")
        self.app.backup_table.selection_set(backup["id"])
        self.app.edit_backup_note()
        dialog = self.app.dialog
        dialog.note.insert("1.0", "第一章完成\n准备挑战最终 Boss")
        dialog.save()
        record = self.app.backups.list_backups(self.game)[0]
        self.assertEqual(record["note"], "第一章完成\n准备挑战最终 Boss")
        self.assertEqual(self.app.backup_table.item(backup["id"], "values")[3], "第一章完成 准备挑战最终 Boss")
        self.assertEqual(self.app.backup_table.selection(), (backup["id"],))
        self.app.edit_backup_note()
        self.assertEqual(self.app.dialog.note.get("1.0", "end-1c"), record["note"])
        self.app.dialog.destroy()

    def test_backup_note_cancel_clear_and_invalid_selection(self):
        first = self.app.backups.create(self.game)
        second = self.app.backups.copy(self.game, first["id"])
        self.app.backups.update_note(self.game, first["id"], "保留备注")
        self.app.refresh()
        self.app.backup_table.selection_set([first["id"], second["id"]])
        self.app.edit_backup_note()
        self.assertIsNone(self.app.dialog)
        self.app.backup_table.selection_set(first["id"])
        self.app.edit_backup_note()
        self.app.dialog.note.insert("end", "取消修改")
        self.app.dialog.destroy()
        self.assertEqual(next(item for item in self.app.backups.list_backups(self.game) if item["id"] == first["id"])["note"], "保留备注")
        self.app.edit_backup_note()
        self.app.dialog.note.delete("1.0", "end")
        self.app.dialog.save()
        self.assertEqual(self.app.backup_table.item(first["id"], "values")[3], "")

    def test_backup_note_save_failure_keeps_editor_for_retry(self):
        record = self.app.backups.create(self.game)
        self.app.refresh()
        self.app.backup_table.selection_set(record["id"])
        self.app.edit_backup_note()
        dialog = self.app.dialog
        dialog.note.insert("1.0", "待保存备注")
        with patch.object(self.app.backups, "update_note", side_effect=PermissionError("文件被占用")):
            dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.note.get("1.0", "end-1c"), "待保存备注")
        self.assertIn("文件被占用", dialog.error.cget("text"))
        self.assertEqual(self.app.backups.list_backups(self.game)[0]["note"], "")
        dialog.save()
        self.assertEqual(self.app.backups.list_backups(self.game)[0]["note"], "待保存备注")

    def test_backup_note_double_click_edits_clicked_row(self):
        from types import SimpleNamespace
        first = self.app.backups.create(self.game)
        second = self.app.backups.copy(self.game, first["id"])
        self.app.refresh()
        self.app.backup_table.selection_set(first["id"])
        with patch.object(self.app.backup_table, "identify_row", return_value=second["id"]):
            self.app.edit_backup_note(SimpleNamespace(y=50))
        self.app.dialog.note.insert("1.0", "第二份备份")
        self.app.dialog.save()
        self.assertEqual(self.app.backup_table.item(second["id"], "values")[3], "第二份备份")
        self.assertEqual(self.app.backup_table.item(first["id"], "values")[3], "")

    def test_game_form_and_settings_save(self):
        from game_manager.ui import SettingsDialog
        self.app.add_game()
        dialog = self.app.dialog
        self.app.add_game()
        self.assertIs(self.app.dialog, dialog)
        dialog.entries["english_name"].insert(0, "New Game")
        dialog.entries["chinese_name"].insert(0, "新游戏")
        dialog.save()
        self.assertEqual(self.app.selected_game()["english_name"], "New Game")
        settings = SettingsDialog(self.app)
        settings.key.insert(0, "test-secret")
        settings.save()
        self.assertEqual(self.app.api_key, "test-secret")
        saved = json.loads((self.root / "data" / "library.json").read_text())
        self.assertEqual(saved["settings"]["api_key"], "")

    @patch("game_manager.ui.steam_name_worker", steam_name_worker, create=True)
    def test_numeric_game_name_resolves_in_background_before_saving(self):
        self.app.add_game()
        dialog = self.app.dialog
        dialog.entries["english_name"].insert(0, " 000570 ")
        dialog.entries["chinese_name"].insert(0, "刀塔")
        dialog.entries["save_path"].insert(0, str(self.save_dir))
        dialog.save()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(len(self.app.storage.games), 1)
        self.assertEqual(dialog.save_button.cget("state"), "disabled")
        self.assertEqual(dialog.steam_job["proxy_url"], "")
        self.wait_until(lambda: len(self.app.storage.games) == 2)
        saved = self.app.selected_game()
        self.assertEqual(saved["english_name"], "Dota 2")
        self.assertEqual(saved["chinese_name"], "刀塔")
        self.assertEqual(saved["save_path"], str(self.save_dir))
        self.assertEqual(saved["steamgrid_id"], "")
        self.assertEqual(Storage(self.root / "data").get_game(saved["id"])["english_name"], "Dota 2")

    @patch("game_manager.ui.steam_name_worker", steam_name_worker)
    def test_edit_numeric_name_uses_resolved_name_to_preserve_or_clear_image_id(self):
        from game_manager.ui import GameDialog
        game = self.app.storage.save_game({**self.game, "english_name": "Dota 2", "steamgrid_id": 73})
        for app_id, expected_name, expected_grid in (("570", "Dota 2", 73), ("123", "Other Game", "")):
            self.app.open_dialog(GameDialog, game)
            dialog = self.app.dialog
            dialog.entries["english_name"].delete(0, "end")
            dialog.entries["english_name"].insert(0, app_id)
            dialog.save()
            self.wait_until(lambda: not dialog.winfo_exists())
            game = self.app.storage.get_game(game["id"])
            self.assertEqual(game["english_name"], expected_name)
            self.assertEqual(game["steamgrid_id"], expected_grid)
            self.assertEqual(game["save_path"], str(self.save_dir))
            self.assertEqual(game["chinese_name"], self.game["chinese_name"])
        self.assertEqual(len(self.app.storage.games), 1)

    def test_steam_name_failure_keeps_form_and_allows_retry(self):
        self.app.add_game()
        dialog = self.app.dialog
        dialog.entries["english_name"].insert(0, "570")
        dialog.entries["save_path"].insert(0, str(self.save_dir))
        with patch("game_manager.ui.steam_name_worker", failed_steam_name_worker):
            dialog.save()
            self.wait_until(lambda: dialog.steam_job is None)
        self.assertEqual(dialog.entries["english_name"].get(), "570")
        self.assertIn("未找到", dialog.error.cget("text"))
        self.assertEqual(len(self.app.storage.games), 1)
        self.assertEqual(dialog.save_button.cget("state"), "normal")
        with patch("game_manager.ui.steam_name_worker", steam_name_worker):
            dialog.save()
            self.wait_until(lambda: not dialog.winfo_exists())
        self.assertEqual(self.app.selected_game()["english_name"], "Dota 2")

    @patch("game_manager.ui.steam_name_worker", steam_name_worker)
    def test_steam_resolved_duplicate_is_rejected_and_numeric_title_saves_once(self):
        self.app.storage.save_game({"english_name": "Dota 2", "save_path": str(self.save_dir)})
        self.app.add_game()
        dialog = self.app.dialog
        dialog.entries["english_name"].insert(0, "570")
        dialog.entries["save_path"].insert(0, str(self.save_dir))
        dialog.save()
        self.wait_until(lambda: dialog.steam_job is None)
        self.assertIn("已存在同名游戏", dialog.error.cget("text"))
        self.assertEqual(len(self.app.storage.games), 2)
        self.assertEqual(dialog.entries["english_name"].get(), "Dota 2")
        dialog.entries["english_name"].delete(0, "end")
        dialog.entries["english_name"].insert(0, "2048")
        dialog.save()
        self.wait_until(lambda: not dialog.winfo_exists())
        self.assertEqual(len(self.app.storage.games), 3)
        self.assertEqual(self.app.selected_game()["english_name"], "2048")

    @patch("game_manager.ui.steam_name_worker", held_steam_name_worker)
    def test_steam_query_blocks_repeated_save_and_window_close_discards_late_result(self):
        self.app.add_game()
        dialog = self.app.dialog
        dialog.entries["english_name"].insert(0, "570")
        dialog.save()
        job = dialog.steam_job
        self.wait_until(lambda: "result" in job)
        self.assertEqual(len(self.app.storage.games), 1)
        dialog.save()
        self.assertIs(dialog.steam_job, job)
        for widget in list(dialog.entries.values()) + dialog.browse_buttons + [dialog.save_button]:
            self.assertEqual(widget.cget("state"), "disabled")
        with patch("game_manager.ui.filedialog.askdirectory") as picker:
            dialog.browse(dialog.entries["save_path"])
            picker.assert_not_called()
        self.app.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
        self.assertIsNone(dialog.steam_job)
        self.assertIsNone(dialog.steam_poll)
        self.assertTrue(job["process"]._closed)
        self.assertTrue(job["receiver"].closed)
        self.app.update()
        self.assertEqual(len(self.app.storage.games), 1)

    @patch("game_manager.ui.steam_name_worker", steam_name_worker)
    @patch("game_manager.ui.pcgw_worker", save_location_worker)
    def test_steam_query_uses_proxy_snapshot_then_pcgamingwiki_uses_resolved_name(self):
        self.app.storage.update_settings({"proxy_enabled": True, "proxy_url": "http://127.0.0.1:7890"})
        self.app.add_game()
        dialog = self.app.dialog
        dialog.entries["english_name"].insert(0, "570")
        dialog.save()
        self.assertEqual(dialog.steam_job["proxy_url"], "http://127.0.0.1:7890")
        self.app.storage.update_settings({"proxy_enabled": False})
        self.wait_until(lambda: not dialog.winfo_exists())
        self.assertEqual(self.app.artwork_job["value"], "Dota 2")
        self.assertEqual(self.app.artwork_job["network"]["proxy_url"], "")
        self.wait_for_artwork()
        self.assertEqual(self.app.selected_game()["save_path"], r"%LOCALAPPDATA%\TestGame\Saved")

    def test_steam_query_start_failure_and_process_exit_keep_original_data(self):
        self.app.add_game()
        dialog = self.app.dialog
        dialog.entries["english_name"].insert(0, "570")
        with patch.object(multiprocessing.process.BaseProcess, "start", side_effect=OSError("start failed")):
            dialog.save()
        self.assertIsNone(dialog.steam_job)
        self.assertIn("无法启动", dialog.error.cget("text"))
        with patch("game_manager.ui.steam_name_worker", empty_steam_name_worker):
            dialog.save()
            job = dialog.steam_job
            self.wait_until(lambda: dialog.steam_job is None)
        self.assertIn("异常退出", dialog.error.cget("text"))
        self.assertEqual(dialog.save_button.cget("state"), "normal")
        self.assertTrue(job["process"]._closed)
        self.assertEqual(dialog.entries["english_name"].get(), "570")
        self.assertEqual(len(self.app.storage.games), 1)

    @patch("game_manager.ui.steam_name_worker", steam_name_worker)
    def test_resolved_steam_name_write_failure_preserves_original_game_and_can_retry(self):
        from game_manager.ui import GameDialog
        game = self.app.storage.save_game({**self.game, "steamgrid_id": 73})
        library = (self.app.storage.data_dir / "library.json").read_bytes()
        self.app.open_dialog(GameDialog, game)
        dialog = self.app.dialog
        dialog.entries["english_name"].delete(0, "end")
        dialog.entries["english_name"].insert(0, "570")
        with patch.object(self.app.storage, "_save", side_effect=OSError("disk full")):
            dialog.save()
            self.wait_until(lambda: dialog.steam_job is None)
        self.assertEqual(self.app.storage.get_game(game["id"]), game)
        self.assertEqual((self.app.storage.data_dir / "library.json").read_bytes(), library)
        self.assertIn("disk full", dialog.error.cget("text"))
        self.assertEqual(dialog.entries["english_name"].get(), "Dota 2")
        dialog.save()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.app.storage.get_game(game["id"])["english_name"], "Dota 2")

    @patch("game_manager.ui.steam_name_worker", held_steam_name_worker)
    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_closing_app_cancels_steam_name_query_alongside_artwork(self):
        self.start_held_download()
        self.app.add_game()
        dialog = self.app.dialog
        dialog.entries["english_name"].insert(0, "570")
        dialog.save()
        job = dialog.steam_job
        self.wait_until(lambda: "result" in job)
        with patch.object(self.app, "destroy") as destroy:
            self.app.close()
            self.wait_for_artwork()
            self.wait_until(lambda: dialog.steam_job is None)
            destroy.assert_called_once()
        self.assertTrue(job["process"]._closed)
        self.assertEqual(len(self.app.storage.games), 1)

    def test_blank_save_path_is_looked_up_and_persisted(self):
        with patch("game_manager.ui.pcgw_worker", save_location_worker, create=True):
            self.app.add_game()
            dialog = self.app.dialog
            dialog.entries["english_name"].insert(0, "Automatic Game")
            dialog.save()
            game_id = self.app.selected_id
            self.wait_until(lambda: self.app.storage.get_game(game_id)["save_path"] != "")
        self.assertEqual(self.app.storage.get_game(game_id)["save_path"], r"%LOCALAPPDATA%\TestGame\Saved")
        self.assertEqual(Storage(self.root / "data").get_game(game_id)["save_path"], r"%LOCALAPPDATA%\TestGame\Saved")

    def test_settings_are_software_wide(self):
        from game_manager.ui import SettingsDialog
        settings = SettingsDialog(self.app)
        self.assertEqual(settings.title(), "软件设置")
        self.assertEqual(self.app.settings_button.cget("text"), "软件设置")
        self.assertEqual(settings.proxy_enabled.cget("text"), "为软件联网请求使用代理")
        settings.destroy()

    def test_switching_games_and_tabs_clears_previous_failure_status(self):
        second = self.app.storage.save_game({"english_name": "Another Game", "save_path": str(self.save_dir)})
        self.app.show_error(RuntimeError("上次获取失败"))
        self.app.select(second["id"])
        self.assertEqual(self.app.status.cget("text"), "就绪")
        self.app.show_error(RuntimeError("再次获取失败"))
        self.app.tabs._segmented_button._buttons_dict["游戏图片"].invoke()
        self.assertEqual(self.app.status.cget("text"), "就绪")

    def test_multiple_pcgw_games_are_chosen_before_loading_save_paths(self):
        from game_manager.ui import GameChoiceDialog
        with patch("game_manager.ui.pcgw_worker", choice_save_game_worker):
            game = self.app.storage.save_game({"english_name": "Selected Game"})
            self.app.select(game["id"])
            self.wait_for_artwork()
            self.assertIsInstance(self.app.dialog, GameChoiceDialog)
            self.assertEqual(self.app.dialog.title(), "选择 PCGamingWiki 匹配文章")
            self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
            self.choose_pcgw_article(202)
            self.wait_for_artwork()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], r"%LOCALAPPDATA%\SelectedGame")
        self.assertEqual(self.app.storage.get_game(game["id"])["english_name"], "Selected Game")

    def test_steam_fallback_choice_confirms_directory_and_preserves_proxy_and_backup(self):
        from customtkinter import CTkButton
        from game_manager.ui import GameChoiceDialog, SaveLocationDialog
        with patch("game_manager.ui.pcgw_worker", choice_steam_cloud_worker):
            self.app.storage.update_settings({"proxy_enabled": True, "proxy_url": "http://127.0.0.1:7890"})
            game = self.app.storage.save_game({"english_name": "Steam Selected|" + str(self.save_dir)})
            self.app.select(game["id"])
            self.app.create_backup()
            self.app.select(self.game["id"])
            self.wait_for_artwork()
            self.assertIsInstance(self.app.dialog, GameChoiceDialog)
            self.assertEqual(self.app.dialog.title(), "选择 Steam 云存档游戏")
            self.app.storage.update_settings({"proxy_enabled": False})
            self.choose_pcgw_article(202)
            self.assertEqual(self.app.artwork_job["value"]["id"], 202)
            self.assertEqual(self.app.artwork_job["network"]["proxy_url"], "http://127.0.0.1:7890")
            self.wait_for_artwork()
            self.assertIsInstance(self.app.dialog, SaveLocationDialog)
            self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
            self.assertEqual(self.app.backups.list_backups(game), [])
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            use_button = next(widget for widget in descendants(self.app.dialog)
                              if isinstance(widget, CTkButton) and widget.cget("text") == "使用此目录")
            use_button.invoke()
            self.wait_for_task()
        self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 0)
        self.assertEqual(self.app.selected_id, self.game["id"])

    def test_steam_directory_confirmation_can_be_cancelled_and_links_to_steamdb(self):
        from customtkinter import CTkButton
        from game_manager.ui import SaveLocationDialog
        game = self.app.storage.save_game({"english_name": "Steam Confirmation"})
        candidate = {"path": r"%APPDATA%\SteamGame", "source": "Steam 云存档", "resolved": True,
                     "label": "Steam 云同步规则 · *.sav", "page_url": "https://steamdb.info/app/202/ufs/"}
        callbacks = []
        self.app.save_location_results(game, [candidate], callbacks.append)
        self.assertIsInstance(self.app.dialog, SaveLocationDialog)
        buttons = [widget for widget in self.app.dialog.winfo_children() if isinstance(widget, CTkButton)]
        source_button = next(widget for widget in buttons if widget.cget("text") == "打开 SteamDB 页面 ↗")
        with patch("game_manager.ui.webbrowser.open") as browser:
            source_button.invoke()
        browser.assert_called_once_with(candidate["page_url"])
        self.app.dialog.destroy()
        self.app.update()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
        self.assertEqual(callbacks, [])

    def test_selected_steam_cloud_lookup_can_be_stopped(self):
        with patch("game_manager.ui.pcgw_worker", held_selected_steam_cloud_worker):
            game = self.app.storage.save_game({"english_name": "Stop Steam Lookup"})
            self.app.select(game["id"])
            self.wait_for_artwork()
            self.choose_pcgw_article(202)
            process = self.app.artwork_job["process"]
            self.assertTrue(process.is_alive())
            self.app.stop_artwork()
            self.wait_for_artwork()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
        self.assertEqual(self.app.save_lookup_queue, [])
        self.assertIn("已停止", self.app.status.cget("text"))

    def test_both_sources_without_windows_paths_show_manual_edit_hint(self):
        game = self.app.storage.save_game({"english_name": "No Steam Rules"})
        self.app.save_location_results(game, {"locations": [], "source": "Steam 云存档"}, None)
        self.assertIsNone(self.app.dialog)
        self.assertIn("Steam", self.app.status.cget("text"))
        self.assertIn("手动填写", self.app.status.cget("text"))
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")

    def test_switching_cards_preserves_active_artwork_status(self):
        second = self.app.storage.save_game({"english_name": "Another Game", "save_path": str(self.save_dir)})
        with patch("game_manager.ui.artwork_worker", held_artwork_worker):
            job = self.start_held_download()
            self.app.select(second["id"])
            self.assertEqual(self.app.status.cget("text"), job["status"])
            self.app.tabs._segmented_button._buttons_dict["游戏图片"].invoke()
            self.assertEqual(self.app.status.cget("text"), job["status"])
            self.app.stop_artwork()
            self.wait_for_artwork()

    def test_pcgw_article_choice_preserves_proxy_and_original_backup_callback(self):
        with patch("game_manager.ui.pcgw_worker", choice_save_game_worker):
            self.app.storage.update_settings({"proxy_enabled": True, "proxy_url": "http://127.0.0.1:7890"})
            game = self.app.storage.save_game({"english_name": "Selected|" + str(self.save_dir)})
            self.app.select(game["id"])
            self.app.create_backup()
            self.app.select(self.game["id"])
            self.wait_for_artwork()
            self.app.storage.update_settings({"proxy_enabled": True, "proxy_url": "http://localhost:8080"})
            self.choose_pcgw_article(202)
            self.assertEqual(self.app.artwork_job["value"]["pageid"], 202)
            self.assertEqual(self.app.artwork_job["network"]["proxy_url"], "http://127.0.0.1:7890")
            self.wait_for_artwork()
            self.wait_for_task()
        self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 0)
        self.assertEqual(self.app.selected_id, self.game["id"])

    def test_pcgw_article_choice_waits_for_other_network_job(self):
        with patch("game_manager.ui.pcgw_worker", choice_save_game_worker), patch("game_manager.ui.artwork_worker", held_artwork_worker):
            game = self.app.storage.save_game({"english_name": "Queued Article|" + str(self.save_dir)})
            self.app.select(game["id"])
            self.app.create_backup()
            self.wait_for_artwork()
            self.app.start_artwork_job("download", self.game, {"api_key": "test-key", "proxy_url": ""}, 42)
            job = self.app.artwork_job
            self.wait_until(lambda: (job["temporary"] / "started").exists())
            self.choose_pcgw_article(202)
            self.assertIs(self.app.artwork_job, job)
            self.assertNotIn("on_resolved", job)
            self.assertEqual(len(self.app.save_lookup_queue), 1)
            (job["temporary"] / "release").touch()
            self.wait_for_artwork()
            self.wait_for_task()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], str(self.save_dir))
        self.assertEqual(len(self.app.backups.list_backups(game)), 1)

    def test_cancel_pcgw_article_choice_does_not_fill_or_backup(self):
        with patch("game_manager.ui.pcgw_worker", choice_save_game_worker):
            game = self.app.storage.save_game({"english_name": "Cancelled Article"})
            self.app.select(game["id"])
            self.app.create_backup()
            self.wait_for_artwork()
            self.app.dialog.destroy()
            self.app.update()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
        self.assertEqual(self.app.backups.list_backups(game), [])
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.save_lookup_queue, [])

    def test_backup_requested_while_choosing_article_uses_selected_article(self):
        with patch("game_manager.ui.pcgw_worker", choice_save_game_worker):
            game = self.app.storage.save_game({"english_name": "Late Backup|" + str(self.save_dir)})
            self.app.select(game["id"])
            self.wait_for_artwork()
            self.app.create_backup()
            self.choose_pcgw_article(202)
            self.wait_for_artwork()
            self.wait_for_task()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], str(self.save_dir))
        self.assertEqual(len(self.app.backups.list_backups(game)), 1)

    def test_pcgw_article_choice_discards_stale_record(self):
        with patch("game_manager.ui.pcgw_worker", choice_save_game_worker):
            for field, value in (("save_path", str(self.save_dir)), ("english_name", "Renamed Article")):
                game = self.app.storage.save_game({"english_name": f"Old Article {field}"})
                self.app.select(game["id"])
                self.wait_for_artwork()
                self.app.storage.save_game({**game, field: value})
                self.choose_pcgw_article(202)
                self.assertIsNone(self.app.artwork_job)
                self.assertEqual(self.app.storage.get_game(game["id"])[field], value)

    def test_pcgw_selected_article_lookup_can_be_stopped(self):
        with patch("game_manager.ui.pcgw_worker", held_selected_save_game_worker):
            game = self.app.storage.save_game({"english_name": "Stop Selected Article"})
            self.app.select(game["id"])
            self.wait_for_artwork()
            self.choose_pcgw_article(202)
            job = self.app.artwork_job
            self.wait_until(lambda: "result" in job)
            self.app.stop_artwork()
            self.wait_for_artwork()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")

    def test_proxy_settings_accept_http_and_https(self):
        from game_manager.ui import SettingsDialog
        for proxy_url, expected in (("https://localhost:7890", "https://localhost:7890"),
                                    ("http://127.0.0.1:7890", "http://127.0.0.1:7890")):
            settings = SettingsDialog(self.app)
            settings.proxy_enabled.select()
            settings.toggle_proxy()
            settings.proxy_url.delete(0, "end")
            settings.proxy_url.insert(0, proxy_url)
            settings.save()
            self.assertEqual(self.app.storage.settings["proxy_url"], expected)
            self.assertTrue(self.app.storage.settings["proxy_enabled"])

    def test_manual_save_path_is_used_without_lookup(self):
        with patch.object(self.app, "start_artwork_job") as start:
            self.app.lookup_save_path(self.game, force=True)
            self.app.create_backup()
            self.wait_for_task()
        start.assert_not_called()
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)

    def test_save_lookup_discards_results_after_manual_edit_or_rename(self):
        with patch("game_manager.ui.pcgw_worker", save_location_worker):
            for field, value in (("save_path", str(self.save_dir)), ("english_name", "Renamed Game")):
                game = self.app.storage.save_game({"english_name": f"Before {field}"})
                self.app.lookup_save_path(game)
                self.app.artwork_job["process"].join(timeout=3)
                self.app.storage.save_game({**game, field: value, "chinese_name": "最新资料"})
                self.wait_for_artwork()
                stored = self.app.storage.get_game(game["id"])
                self.assertEqual(stored[field], value)
                self.assertEqual(stored["chinese_name"], "最新资料")
                if field == "english_name":
                    self.assertEqual(stored["save_path"], "")

    def test_candidate_paths_require_selection_and_preserve_latest_fields(self):
        from game_manager.ui import SaveLocationDialog
        game = self.app.storage.save_game({"english_name": "Candidate Game"})
        candidates = [{"label": "Steam", "path": r"<Steam-folder>\userdata\<user-id>\123",
                       "page_url": "https://www.pcgamingwiki.com/wiki/Candidate_Game", "resolved": False},
                      {"label": "Windows", "path": r"%LOCALAPPDATA%\CandidateGame",
                       "page_url": "https://www.pcgamingwiki.com/wiki/Candidate_Game", "resolved": True}]
        self.app.save_location_results(game, candidates, None)
        self.assertIsInstance(self.app.dialog, SaveLocationDialog)
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
        self.app.dialog.destroy()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
        self.app.save_location_results(game, candidates, None)
        self.app.storage.save_game({**game, "chinese_name": "修改后资料"})
        self.app.dialog.choose(candidates[1], lambda candidate: self.app.use_save_location(game, candidate, None))
        stored = self.app.storage.get_game(game["id"])
        self.assertEqual(stored["save_path"], candidates[1]["path"])
        self.assertEqual(stored["chinese_name"], "修改后资料")

    def test_save_location_contents_allow_partial_copy_without_editing_or_confirming(self):
        from tkinter import TclError
        from customtkinter import CTkButton, CTkTextbox
        from game_manager.ui import SaveLocationDialog

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        try:
            original_clipboard = self.app.clipboard_get()
        except TclError:
            original_clipboard = None
        try:
            for source in ("PCGamingWiki", "Steam 云存档"):
                game = self.app.storage.save_game({"english_name": f"Copy Paths {source}"})
                candidates = [{"label": "Windows · 文件规则：*.sav", "path": r"%LOCALAPPDATA%\中文游戏\Saved",
                               "source": source, "resolved": True, "page_url": "https://example.invalid/game"},
                              {"label": "Steam 账号目录", "path": "<Steam-folder>\\userdata\\<user-id>\\"
                               + "long-directory-" * 25, "source": source, "resolved": False,
                               "page_url": "https://example.invalid/game"}]
                callbacks = []
                self.app.open_dialog(SaveLocationDialog, game, candidates, callbacks.append)
                dialog = self.app.dialog
                textboxes = [widget for widget in descendants(dialog) if isinstance(widget, CTkTextbox)]
                self.assertEqual(len(textboxes), 2)
                for textbox, candidate in zip(textboxes, candidates):
                    text = f"{candidate['label']}\n{candidate['path']}"
                    self.assertEqual(textbox.get("1.0", "end-1c"), text)
                    self.assertEqual(textbox._textbox.cget("state"), "disabled")
                    textbox.insert("end", "不应修改")
                    textbox.delete("1.0", "end")
                    self.assertEqual(textbox.get("1.0", "end-1c"), text)
                    textbox.tag_add("sel", "2.0", "2.14")
                    self.app.clipboard_clear()
                    self.app.clipboard_append("copy-test-placeholder")
                    textbox._textbox.event_generate("<<Copy>>")
                    self.assertEqual(self.app.clipboard_get(), candidate["path"][:14])
                    textbox.tag_remove("sel", "1.0", "end")
                    textbox.tag_add("sel", "1.0", "end-1c")
                    textbox._textbox.event_generate("<<Copy>>")
                    self.assertEqual(self.app.clipboard_get(), text)
                    self.assertEqual(callbacks, [])
                    self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
                buttons = [widget for widget in descendants(dialog) if isinstance(widget, CTkButton)
                           and widget.cget("text") in ("使用此目录", "需手动填写")]
                self.assertEqual([button.cget("state") for button in buttons], ["normal", "disabled"])
                buttons[0].invoke()
                self.assertEqual(callbacks, [candidates[0]])
        finally:
            self.app.clipboard_clear()
            if original_clipboard is not None:
                self.app.clipboard_append(original_clipboard)

    def test_save_lookup_uses_global_proxy_and_needs_no_api_key(self):
        with patch("game_manager.ui.pcgw_worker", proxy_save_location_worker):
            for enabled, url, expected in ((False, "invalid stored proxy", "Direct"),
                                           (True, "http://127.0.0.1:7890", "Proxied")):
                self.app.storage.update_settings({"proxy_enabled": enabled, "proxy_url": url})
                game = self.app.storage.save_game({"english_name": f"Proxy {enabled}"})
                self.app.api_key = ""
                self.app.lookup_save_path(game)
                self.wait_for_artwork()
                self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "%LOCALAPPDATA%\\" + expected)

    def test_blank_backup_attaches_to_lookup_and_keeps_original_game(self):
        with patch("game_manager.ui.pcgw_worker", local_save_location_worker):
            game = self.app.storage.save_game({"english_name": "Source|" + str(self.save_dir)})
            self.app.select(game["id"])
            job = self.app.artwork_job
            self.app.create_backup()
            self.app.create_backup()
            self.assertIs(self.app.artwork_job, job)
            self.app.select(self.game["id"])
            self.wait_for_artwork()
            self.wait_for_task()
        self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 0)
        self.assertEqual(self.app.selected_id, self.game["id"])

    def test_blank_restore_keeps_original_game_and_backup(self):
        from game_manager.ui import messagebox
        record = self.app.backups.create(self.game)
        (self.save_dir / "slot.dat").write_text("changed", encoding="utf-8")
        game = self.app.storage.save_game({**self.game, "english_name": "Restore|" + str(self.save_dir), "save_path": ""})
        second = self.app.storage.save_game({"english_name": "Other Game", "save_path": str(self.root / "other")})
        self.app.refresh()
        self.app.backup_table.selection_set(record["id"])
        with patch("game_manager.ui.pcgw_worker", local_save_location_worker):
            self.app.restore_backup()
            self.app.select(second["id"])
            self.wait_for_artwork()
            self.wait_for_task()
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")
        self.assertEqual(len(self.app.backups.list_backups(game)), 1)
        self.assertIn(game["english_name"], messagebox.askyesno.call_args.args[1])
        self.assertEqual(self.app.selected_id, second["id"])

    def test_lookup_queue_runs_after_images_and_can_be_cancelled_then_retried(self):
        with patch("game_manager.ui.artwork_worker", held_artwork_worker), patch("game_manager.ui.pcgw_worker", save_location_worker):
            job = self.start_held_download()
            first = self.app.storage.save_game({"english_name": "Queued Game"})
            self.app.lookup_save_path(first)
            self.assertEqual(len(self.app.save_lookup_queue), 1)
            self.assertIs(self.app.artwork_job, job)
            (job["temporary"] / "release").touch()
            self.wait_until(lambda: self.app.storage.get_game(first["id"])["save_path"] != "")
            job = self.start_held_download()
            second = self.app.storage.save_game({"english_name": "Cancelled Game"})
            self.app.lookup_save_path(second)
            self.app.stop_artwork()
            self.wait_for_artwork()
            self.assertEqual(self.app.save_lookup_queue, [])
            self.assertEqual(self.app.storage.get_game(second["id"])["save_path"], "")
            self.app.lookup_save_path(second, force=True)
            self.wait_for_artwork()
            self.assertNotEqual(self.app.storage.get_game(second["id"])["save_path"], "")

    def test_stop_and_close_terminate_save_lookup_without_filling_path(self):
        with patch("game_manager.ui.pcgw_worker", held_save_location_worker):
            game = self.app.storage.save_game({"english_name": "Stopped Lookup"})
            self.app.lookup_save_path(game)
            job = self.app.artwork_job
            self.wait_until(lambda: "result" in job)
            self.assertEqual(self.app.stop_artwork_button.cget("text"), "停止查询")
            self.app.stop_artwork()
            self.wait_for_artwork()
            self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
            self.assertFalse(job["temporary"].exists())
            self.app.lookup_save_path(game, force=True)
            process = self.app.artwork_job["process"]
            with patch.object(self.app, "destroy") as destroy:
                self.app.close()
                self.wait_until(lambda: self.app.artwork_job is None)
                destroy.assert_called_once()
            self.assertTrue(process._closed)
            self.assertEqual(Storage(self.root / "data").get_game(game["id"])["save_path"], "")

    def test_candidate_write_failure_is_reported_without_changing_path(self):
        from game_manager.ui import messagebox
        game = self.app.storage.save_game({"english_name": "Write Failure"})
        candidate = {"label": "Windows", "path": r"%LOCALAPPDATA%\WriteFailure", "resolved": True,
                     "page_url": "https://www.pcgamingwiki.com/wiki/Write_Failure"}
        self.app.save_location_results(game, [candidate, {**candidate, "label": "Steam"}], None)
        with patch.object(self.app.storage, "save_game", side_effect=OSError("disk full")):
            self.app.dialog.choose(candidate, lambda item: self.app.use_save_location(game, item, None))
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
        self.assertIn("disk full", messagebox.showerror.call_args.args[1])

    def test_close_during_data_operation_cancels_lookup_and_queue(self):
        with patch("game_manager.ui.pcgw_worker", held_save_location_worker):
            game = self.app.storage.save_game({"english_name": "Closing Lookup"})
            self.app.lookup_save_path(game)
            job = self.app.artwork_job
            self.app.run_task("数据操作", lambda: time.sleep(0.15), lambda _: None)
            pending = self.app.storage.save_game({"english_name": "Pending Lookup"})
            self.app.lookup_save_path(pending)
            self.app.close()
            self.assertEqual(self.app.save_lookup_queue, [])
            self.assertTrue(job["cancelled"])
            self.wait_for_task()
            self.wait_for_artwork()
        self.assertEqual(self.app.storage.get_game(game["id"])["save_path"], "")
        self.assertEqual(self.app.storage.get_game(pending["id"])["save_path"], "")

    def test_four_artwork_previews_render(self):
        from PIL import Image
        artwork = self.app.storage.artwork_dir(self.game)
        artwork.mkdir(parents=True)
        assets = {}
        for kind, size in (("cover", (600, 900)), ("wide", (920, 430)),
                           ("hero", (1920, 620)), ("logo", (320, 100))):
            filename = f"{kind}.png"
            Image.new("RGBA", size, "#5b83f6").save(artwork / filename)
            assets[kind] = {"file": filename}
        (artwork / "assets.json").write_text(json.dumps({"assets": assets, "missing": []}))
        self.app.refresh()
        self.app.tabs.set("游戏图片")
        self.app.update_idletasks()
        self.assertEqual(len(self.app.images), 5)

    def test_local_artwork_change_copies_file_and_keeps_other_types(self):
        from PIL import Image
        folder = self.app.storage.artwork_dir(self.game)
        folder.mkdir(parents=True)
        Image.new("RGB", (600, 900), "red").save(folder / "cover.png")
        Image.new("RGB", (1920, 620), "green").save(folder / "hero.png")
        hero = (folder / "hero.png").read_bytes()
        (folder / "assets.json").write_text(json.dumps({"game_id": 42, "notes": "keep", "assets": {
            "cover": {"file": "cover.png"}, "hero": {"file": "hero.png"}}, "missing": ["logo"]}))
        self.app.storage.save_game({**self.game, "steamgrid_id": 42})
        selected = self.root / "手选封面.jpg"
        Image.new("RGB", (123, 234), "blue").save(selected)
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(selected)):
            self.app.change_artwork(self.game, "cover")
            self.wait_for_task()
        manifest = json.loads((folder / "assets.json").read_text())
        self.assertEqual((folder / manifest["assets"]["cover"]["file"]).read_bytes(), selected.read_bytes())
        self.assertFalse((folder / "cover.png").exists())
        self.assertEqual((folder / "hero.png").read_bytes(), hero)
        self.assertEqual(manifest["notes"], "keep")
        self.assertEqual(manifest["missing"], ["logo"])
        self.assertEqual(self.app.storage.get_game(self.game["id"])["steamgrid_id"], 42)

    def test_each_artwork_type_has_change_and_search_controls(self):
        self.assertEqual(len(self.app.artwork_controls), 8)
        self.assertEqual([button.cget("text") for button in self.app.artwork_controls], ["更改", "再搜索"] * 4)

    def test_cancelled_or_invalid_local_image_keeps_existing_artwork(self):
        folder, original = self.seed_artwork()
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=""):
            self.app.change_artwork(self.game, "cover")
        self.assertFalse(self.app.busy)
        invalid = self.root / "invalid.png"
        invalid.write_bytes(b"not an image")
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(invalid)), \
                patch("game_manager.ui.messagebox.showerror") as error:
            self.app.change_artwork(self.game, "cover")
            self.wait_for_task()
            error.assert_called_once()
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)

    @patch("game_manager.ui.artwork_worker", gallery_artwork_worker)
    def test_artwork_gallery_previews_paging_and_cancel_clean_temporary_files(self):
        from game_manager.ui import ArtworkChoiceDialog
        folder, original = self.seed_artwork()
        self.app.storage.save_game({**self.game, "steamgrid_id": 42})
        self.app.api_key = "test-key"
        self.app.search_artwork_kind(self.game, "cover")
        self.wait_for_artwork()
        first = self.app.dialog
        self.assertIsInstance(first, ArtworkChoiceDialog)
        self.assertEqual(len(first.images), 2)
        self.assertEqual(first.previous_button.cget("state"), "disabled")
        self.assertEqual(first.next_button.cget("state"), "normal")
        first.next_button.invoke()
        self.assertFalse(first.temporary.exists())
        self.assertEqual(self.app.artwork_job["value"]["page"], 1)
        self.wait_for_artwork()
        second = self.app.dialog
        self.assertEqual(set(second.select_buttons), {102, 103})
        self.assertEqual(second.next_button.cget("state"), "disabled")
        second.previous_button.invoke()
        self.assertFalse(second.temporary.exists())
        self.wait_for_artwork()
        final = self.app.dialog
        self.assertEqual(final.result["page"], 0)
        final.destroy()
        self.assertFalse(final.temporary.exists())
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)

    @patch("game_manager.ui.artwork_worker", gallery_artwork_worker)
    def test_selected_single_image_keeps_other_types_and_original_game_binding(self):
        folder, original = self.seed_artwork()
        self.app.storage.save_game({**self.game, "steamgrid_id": 42})
        second = self.app.storage.save_game({"english_name": "Second Game", "save_path": str(self.save_dir)})
        self.app.storage.update_settings({"proxy_enabled": True, "proxy_url": "http://127.0.0.1:7890"})
        self.app.api_key = "test-key"
        self.app.artwork_controls[3].invoke()
        self.wait_for_artwork()
        dialog = self.app.dialog
        self.app.select(second["id"])
        self.app.storage.update_settings({"proxy_enabled": False})
        dialog.select_buttons[101].invoke()
        self.assertFalse(dialog.temporary.exists())
        job = self.app.artwork_job
        self.assertEqual(job["action"], "download_one")
        self.assertEqual(job["game"]["id"], self.game["id"])
        self.assertEqual(job["network"]["proxy_url"], "http://127.0.0.1:7890")
        self.wait_for_artwork()
        manifest = json.loads((folder / "assets.json").read_text())
        self.assertEqual(manifest["assets"]["wide"]["id"], 101)
        self.assertEqual(manifest["notes"], "keep")
        self.assertEqual(manifest["missing"], ["logo"])
        for name in ("cover.png", "hero.png"):
            self.assertEqual((folder / name).read_bytes(), original[name])
        self.assertEqual(self.app.selected_id, second["id"])
        self.assertFalse(self.app.storage.artwork_dir(second).exists())
        saved = self.app.storage.get_game(self.game["id"])
        self.assertEqual(saved["steamgrid_id"], 42)
        with zipfile.ZipFile(self.app.backups.export(saved)) as archive:
            self.assertEqual(archive.read("artwork/wide.jpg"), (folder / "wide.jpg").read_bytes())

    @patch("game_manager.ui.artwork_worker", choice_gallery_artwork_worker)
    def test_single_image_search_chooses_matching_game_before_gallery(self):
        from game_manager.ui import ArtworkChoiceDialog, GameChoiceDialog
        self.app.api_key = "test-key"
        self.app.search_artwork_kind(self.game, "logo")
        self.assertEqual(self.app.artwork_job["action"], "search")
        self.wait_for_artwork()
        self.assertIsInstance(self.app.dialog, GameChoiceDialog)
        self.choose_pcgw_article(43)
        self.assertEqual(self.app.artwork_job["value"], {"game_id": 43, "kind": "logo", "page": 0})
        self.wait_for_artwork()
        self.assertIsInstance(self.app.dialog, ArtworkChoiceDialog)
        self.app.dialog.select_buttons[100].invoke()
        self.wait_for_artwork()
        folder = self.app.storage.artwork_dir(self.game)
        manifest = json.loads((folder / "assets.json").read_text())
        self.assertEqual(set(manifest["assets"]), {"logo"})
        self.assertEqual(self.app.storage.get_game(self.game["id"])["steamgrid_id"], 43)

    @patch("game_manager.ui.artwork_worker", gallery_artwork_worker)
    def test_single_image_configuration_failure_rolls_back_files_and_metadata(self):
        folder, original = self.seed_artwork()
        self.app.storage.save_game({**self.game, "steamgrid_id": 42})
        library = (self.app.storage.data_dir / "library.json").read_bytes()
        self.app.download_single_artwork(self.game, {"api_key": "test-key", "proxy_url": ""}, 43, "cover", {"id": 100})
        job = self.app.artwork_job
        job["process"].join(timeout=3)
        self.assertFalse(job["process"].is_alive())
        with patch.object(self.app.storage, "_save", side_effect=OSError("configuration write failed")), \
                patch("game_manager.ui.messagebox.showerror") as error:
            self.wait_for_artwork()
            error.assert_called_once()
        self.assertFalse(job["temporary"].exists())
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        self.assertEqual((self.app.storage.data_dir / "library.json").read_bytes(), library)
        self.assertEqual(self.app.storage.get_game(self.game["id"])["steamgrid_id"], 42)

    def test_clear_artwork_removes_all_saved_images_and_keeps_backups_and_source(self):
        from PIL import Image
        from game_manager.artwork import replace_local_asset
        folder, _ = self.seed_artwork()
        source = self.root / "用户原图.png"
        Image.new("RGB", (80, 80), "green").save(source)
        replace_local_asset("logo", source, folder)
        backup = self.app.backups.create(self.game)
        second = self.add_archived_game("Other Images")
        other_image = self.app.storage.artwork_dir(second) / "hero.png"
        other_image.parent.mkdir(parents=True)
        Image.new("RGB", (1920, 620), "blue").save(other_image)
        other_bytes = other_image.read_bytes()
        self.app.storage.save_game({**self.game, "steamgrid_id": 42})
        self.app.refresh()
        self.app.clear_artwork_button.invoke()
        self.wait_for_task()
        self.assertEqual(list(folder.iterdir()), [])
        self.assertTrue(source.is_file())
        self.assertEqual(self.app.storage.get_game(self.game["id"])["steamgrid_id"], 42)
        self.assertEqual(self.app.backups.list_backups(self.game), [backup])
        self.assertEqual(other_image.read_bytes(), other_bytes)
        self.assertEqual(self.app.artwork_manifest(self.game).get("assets", {}), {})
        self.assertIn("已清空", self.app.status.cget("text"))
        with zipfile.ZipFile(self.app.backups.export(self.game)) as package:
            self.assertFalse(any(name.startswith("artwork/") for name in package.namelist()))

    def test_clear_artwork_cancel_or_write_failure_keeps_images(self):
        folder, original = self.seed_artwork()
        with patch("game_manager.ui.messagebox.askyesno", return_value=False):
            self.app.clear_artwork(self.game)
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        with patch("game_manager.ui.clear_assets", side_effect=PermissionError("文件被占用")):
            self.app.clear_artwork(self.game)
            self.wait_for_task()
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        self.assertFalse(self.app.busy)

    def test_clear_artwork_is_disabled_during_fetch_or_open_gallery(self):
        folder, original = self.seed_artwork()
        with patch("game_manager.ui.artwork_worker", held_artwork_worker):
            self.start_held_download()
            self.assertEqual(self.app.clear_artwork_button.cget("state"), "disabled")
            with patch("game_manager.ui.messagebox.askyesno") as confirm:
                self.app.clear_artwork(self.game)
            confirm.assert_not_called()
            self.app.stop_artwork()
            self.wait_for_artwork()
        self.app.storage.save_game({**self.game, "steamgrid_id": 42})
        with patch("game_manager.ui.artwork_worker", gallery_artwork_worker):
            self.app.search_artwork_kind(self.game, "cover")
            self.wait_for_artwork()
            with patch("game_manager.ui.messagebox.askyesno") as confirm:
                self.app.clear_artwork(self.game)
            confirm.assert_not_called()
            self.app.dialog.destroy()
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)

    def test_gallery_and_single_image_tasks_can_stop_and_block_manual_changes(self):
        folder, original = self.seed_artwork()
        value = {"game_id": 42, "kind": "cover", "page": 0, "candidate": {"id": 100}}
        with patch("game_manager.ui.artwork_worker", gallery_artwork_worker):
            for action in ("gallery", "download_one"):
                with self.subTest(action=action):
                    self.app.start_artwork_job(action, self.game, {"api_key": "held", "proxy_url": ""}, value)
                    job = self.app.artwork_job
                    self.wait_until(lambda: (job["temporary"] / "started").exists())
                    self.assertTrue(all(button.cget("state") == "disabled" for button in self.app.artwork_controls))
                    with patch("game_manager.ui.filedialog.askopenfilename") as picker:
                        self.app.change_artwork(self.game, "cover")
                        picker.assert_not_called()
                    self.app.stop_artwork()
                    self.wait_for_artwork()
                    self.assertFalse(job["temporary"].exists())
                    self.assertTrue(all(button.cget("state") == "normal" for button in self.app.artwork_controls))
                    self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        with patch("game_manager.ui.artwork_worker", empty_artwork_worker), \
                patch("game_manager.ui.messagebox.showerror") as error:
            self.app.start_artwork_job("gallery", self.game, {"api_key": "test-key", "proxy_url": ""}, value)
            temporary = self.app.artwork_job["temporary"]
            self.wait_for_artwork()
            error.assert_called_once()
        self.assertFalse(temporary.exists())
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)

    @patch("game_manager.ui.artwork_worker", gallery_artwork_worker)
    def test_closing_app_with_gallery_cleans_previews_and_keeps_existing_artwork(self):
        from game_manager.ui import GameManagerApp
        folder, original = self.seed_artwork()
        self.app.load_artwork_gallery(self.game, {"api_key": "test-key", "proxy_url": ""}, 42, "cover")
        self.wait_for_artwork()
        temporary = self.app.dialog.temporary
        self.app.close()
        self.assertFalse(temporary.exists())
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        self.app = GameManagerApp(self.root / "data")
        self.app.withdraw()
        self.app.update()

    @patch("game_manager.ui.artwork_worker", gallery_artwork_worker)
    def test_gallery_window_failure_cleans_previews_and_keeps_existing_artwork(self):
        folder, original = self.seed_artwork()
        self.app.load_artwork_gallery(self.game, {"api_key": "test-key", "proxy_url": ""}, 42, "cover")
        temporary = self.app.artwork_job["temporary"]
        with patch("game_manager.ui.ArtworkChoiceDialog", side_effect=RuntimeError("dialog failed")), \
                patch("game_manager.ui.messagebox.showerror") as error:
            self.wait_for_artwork()
            error.assert_called_once()
        self.assertFalse(temporary.exists())
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)

    def test_artwork_controls_are_accessible_at_minimum_window_size(self):
        folder = self.app.storage.artwork_dir(self.game)
        folder.mkdir(parents=True)
        (folder / "assets.json").write_text(json.dumps({"assets": {}, "missing": ["cover", "wide", "hero", "logo"]}))
        self.app.refresh()
        self.app.deiconify()
        self.app.geometry("1020x720")
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.app.tabs._segmented_button._buttons_dict["游戏图片"].invoke()
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        from customtkinter import CTkScrollableFrame
        tab = self.app.tabs.tab("游戏图片")
        ancestor = self.app.artwork_controls[0].master
        while ancestor is not None and not isinstance(ancestor, CTkScrollableFrame):
            ancestor = ancestor.master
        if ancestor is not None:
            scroll = ancestor
            scroll._parent_canvas.yview_moveto(1)
            self.app.update()
        bottom = tab.winfo_rooty() + tab.winfo_height()
        for button in self.app.artwork_controls:
            self.assertTrue(button.winfo_ismapped())
            self.assertGreaterEqual(button.winfo_rooty(), tab.winfo_rooty())
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom)

    def test_banner_keeps_all_image_edges_visible_after_resize(self):
        from PIL import Image, ImageDraw
        artwork = self.app.storage.artwork_dir(self.game)
        artwork.mkdir(parents=True)
        image = Image.new("RGB", (1920, 620), "#00aa00")
        ImageDraw.Draw(image).rectangle((0, 0, 1919, 619), outline="#ff0000", width=50)
        image.save(artwork / "hero.png")
        (artwork / "assets.json").write_text(json.dumps({"assets": {"hero": {"file": "hero.png"}}}))
        self.app.refresh()
        self.app.deiconify()
        import customtkinter as ctk
        self.addCleanup(ctk.set_widget_scaling, 1.0)
        for scaling in (1.0, 1.5):
            ctk.set_widget_scaling(scaling)
            for width in (1220, 1020, 1450):
                with self.subTest(scaling=scaling, width=width):
                    self.app.geometry(f"{width}x830")
                    deadline = time.monotonic() + 0.2
                    while time.monotonic() < deadline:
                        self.app.update()
                        time.sleep(0.01)
                    banner = self.app.content.grid_slaves(row=1, column=0)[0]
                    photo = banner._label.cget("image")
                    image_width = int(banner.tk.call("image", "width", photo))
                    image_height = int(banner.tk.call("image", "height", photo))
                    for x, y in ((image_width // 2, 2), (image_width // 2, image_height - 3),
                                 (2, image_height // 2), (image_width - 3, image_height // 2)):
                        self.assertEqual(tuple(banner.tk.call(photo, "get", x, y))[:3], (255, 0, 0))
                    self.assertGreaterEqual(banner._label.winfo_x(), 0)
                    self.assertGreaterEqual(banner._label.winfo_y(), 0)
                    self.assertLessEqual(banner._label.winfo_x() + image_width, banner.winfo_width())
                    self.assertLessEqual(banner._label.winfo_y() + image_height, banner.winfo_height())

    def test_import_from_empty_library_and_repeat_through_ui(self):
        from game_manager.backups import BackupManager
        self.app.backups.create(self.game)
        exported = self.app.backups.export(self.game)
        self.app.storage = Storage(self.root / "imported-data")
        self.app.backups = BackupManager(self.app.storage)
        self.app.selected_id = None
        self.app.refresh()
        self.assertEqual(self.app.import_button.cget("state"), "normal")
        (self.save_dir / "slot.dat").write_text("current", encoding="utf-8")
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(exported)):
            self.app.import_archive()
            self.wait_for_task()
            self.assertEqual(self.app.selected_game()["id"], self.game["id"])
            self.assertEqual(len(self.app.backup_table.get_children()), 1)
            self.assertEqual((self.save_dir / "slot.dat").read_text(), "current")
            self.app.import_archive()
            self.wait_for_task()
            self.assertEqual(len(self.app.backup_table.get_children()), 1)
            self.assertIn("跳过 1 条", self.app.status.cget("text"))
        imported_id = self.app.backup_table.get_children()[0]
        self.app.backup_table.selection_set(imported_id)
        self.app.restore_backup()
        self.wait_for_task()
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")

    def test_import_cancel_and_invalid_package_leave_library_unchanged(self):
        self.app.backups.create(self.game)
        exported = self.app.backups.export(self.game)
        library = (self.root / "data" / "library.json").read_bytes()
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(exported)), \
                patch("game_manager.ui.messagebox.askyesno", return_value=False):
            self.app.import_archive()
            self.wait_for_task()
        self.assertEqual((self.root / "data" / "library.json").read_bytes(), library)
        self.assertEqual(self.app.status.cget("text"), "已取消导入。")
        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip")
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(broken)), \
                patch("game_manager.ui.messagebox.showerror") as error:
            self.app.import_archive()
            self.wait_for_task()
            error.assert_called_once()
        self.assertEqual((self.root / "data" / "library.json").read_bytes(), library)
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "original")

    def test_proxy_settings_persist_and_disabled_mode_keeps_address(self):
        from game_manager.ui import SettingsDialog
        settings = SettingsDialog(self.app)
        settings.proxy_enabled.select()
        settings.toggle_proxy()
        settings.proxy_url.insert(0, "http://127.0.0.1:7890")
        settings.save()
        stored = Storage(self.root / "data").settings
        self.assertTrue(stored["proxy_enabled"])
        self.assertEqual(stored["proxy_url"], "http://127.0.0.1:7890")
        settings = SettingsDialog(self.app)
        self.assertEqual(settings.proxy_enabled.get(), 1)
        settings.proxy_enabled.deselect()
        settings.toggle_proxy()
        self.assertEqual(settings.proxy_url.cget("state"), "disabled")
        settings.save()
        stored = Storage(self.root / "data").settings
        self.assertFalse(stored["proxy_enabled"])
        self.assertEqual(stored["proxy_url"], "http://127.0.0.1:7890")

    def test_invalid_enabled_proxy_does_not_save_settings(self):
        from game_manager.ui import SettingsDialog
        settings = SettingsDialog(self.app)
        settings.proxy_enabled.select()
        settings.toggle_proxy()
        settings.proxy_url.insert(0, "ftp://127.0.0.1:1080")
        previous = (self.root / "data" / "library.json").read_bytes()
        with patch("game_manager.ui.messagebox.showerror") as error:
            settings.save()
            error.assert_called_once()
        self.assertTrue(settings.winfo_exists())
        self.assertEqual((self.root / "data" / "library.json").read_bytes(), previous)
        settings.destroy()

    def test_legacy_socks_proxy_is_rejected_without_saving(self):
        from game_manager.ui import SettingsDialog
        for protocol in ("socket", "socks", "socks4", "socks4a", "socks5", "socks5h"):
            with self.subTest(protocol=protocol):
                settings = SettingsDialog(self.app)
                settings.proxy_enabled.select()
                settings.toggle_proxy()
                settings.proxy_url.insert(0, f"{protocol}://127.0.0.1:7890")
                previous = (self.root / "data" / "library.json").read_bytes()
                with patch("game_manager.ui.messagebox.showerror") as error:
                    settings.save()
                self.assertIn("HTTP", error.call_args.args[1])
                self.assertIn("HTTPS", error.call_args.args[1])
                self.assertTrue(settings.winfo_exists())
                self.assertEqual((self.root / "data" / "library.json").read_bytes(), previous)
                settings.destroy()

    def test_disabling_legacy_socks_proxy_saves_and_uses_direct_connection(self):
        from game_manager.network import settings_proxy_url
        from game_manager.ui import SettingsDialog
        self.app.storage.update_settings({"proxy_enabled": True, "proxy_url": "socks5h://127.0.0.1:7890"})
        settings = SettingsDialog(self.app)
        settings.proxy_enabled.deselect()
        settings.toggle_proxy()
        with patch("game_manager.ui.messagebox.showerror") as error:
            settings.save()
        error.assert_not_called()
        stored = Storage(self.root / "data").settings
        self.assertFalse(stored["proxy_enabled"])
        self.assertEqual(stored["proxy_url"], "socks5h://127.0.0.1:7890")
        self.assertEqual(settings_proxy_url(stored), "")

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_artwork_process_keeps_ui_and_backups_available_and_stops(self):
        previous = self.app.storage.artwork_dir(self.game)
        previous.mkdir(parents=True)
        (previous / "old.png").write_bytes(b"old artwork")
        second = self.app.storage.save_game({"english_name": "Second Game"})
        self.app.refresh()
        job = self.start_held_download()
        process_id = job["process"].pid
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.add_button.cget("state"), "normal")
        self.assertEqual(self.app.stop_artwork_button.cget("state"), "normal")
        self.app.create_backup()
        self.wait_for_task()
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)
        self.app.select(second["id"])
        self.assertEqual(self.app.selected_id, second["id"])
        self.app.stop_artwork()
        self.wait_for_artwork()
        self.assertNotIn(process_id, [process.pid for process in multiprocessing.active_children()])
        self.assertEqual((previous / "old.png").read_bytes(), b"old artwork")
        self.assertFalse((previous / "hero.png").exists())
        self.assertFalse(job["temporary"].exists())
        self.assertIn("已停止", self.app.status.cget("text"))

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_artwork_completion_preserves_selection_and_current_game_fields(self):
        second = self.app.storage.save_game({"english_name": "Second Game"})
        job = self.start_held_download()
        changed = self.app.storage.get_game(self.game["id"])
        changed["chinese_name"] = "下载期间修改的名称"
        changed["save_path"] = str(self.root / "new-save")
        self.app.storage.save_game(changed)
        self.app.select(second["id"])
        (job["temporary"] / "release").touch()
        self.wait_for_artwork()
        current = self.app.storage.get_game(self.game["id"])
        self.assertEqual(current["chinese_name"], changed["chinese_name"])
        self.assertEqual(current["save_path"], changed["save_path"])
        self.assertEqual(current["steamgrid_id"], 42)
        self.assertEqual(self.app.selected_id, second["id"])
        self.assertTrue((self.app.storage.artwork_dir(self.game) / "hero.png").is_file())
        self.assertFalse(job["temporary"].exists())

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_artwork_for_renamed_game_is_discarded(self):
        job = self.start_held_download()
        changed = self.app.storage.get_game(self.game["id"])
        changed["english_name"] = "Renamed Game"
        self.app.storage.save_game(changed)
        (job["temporary"] / "release").touch()
        self.wait_for_artwork()
        self.assertFalse(self.app.storage.artwork_dir(self.game).exists())
        self.assertEqual(self.app.storage.get_game(self.game["id"])["steamgrid_id"], "")
        self.assertIn("英文名已更改", self.app.status.cget("text"))

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_close_stops_artwork_process_before_destroying_window(self):
        job = self.start_held_download()
        process_id = job["process"].pid
        with patch.object(self.app, "destroy") as destroy:
            self.app.close()
            self.assertEqual(self.app.add_button.cget("state"), "disabled")
            self.app.create_backup()
            self.assertFalse(self.app.busy)
            self.wait_for_artwork()
            destroy.assert_called_once()
        self.assertFalse(job["temporary"].exists())
        self.assertNotIn(process_id, [process.pid for process in multiprocessing.active_children()])

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_stop_discards_success_that_arrived_before_poll(self):
        old = self.app.storage.artwork_dir(self.game)
        old.mkdir(parents=True)
        (old / "old.png").write_bytes(b"previous")
        job = self.start_held_download()
        (job["temporary"] / "release").touch()
        job["process"].join(timeout=3)
        self.assertFalse(job["process"].is_alive())
        self.app.stop_artwork()
        self.wait_for_artwork()
        self.assertEqual((old / "old.png").read_bytes(), b"previous")
        self.assertFalse((old / "hero.png").exists())
        self.assertEqual(self.app.storage.get_game(self.game["id"])["steamgrid_id"], "")

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_artwork_configuration_failure_restores_previous_images(self):
        old = self.app.storage.artwork_dir(self.game)
        old.mkdir(parents=True)
        (old / "old.png").write_bytes(b"previous")
        library = (self.root / "data" / "library.json").read_bytes()
        job = self.start_held_download()
        with patch.object(self.app.storage, "_save", side_effect=OSError("configuration write failed")), \
                patch("game_manager.ui.messagebox.showerror") as error:
            (job["temporary"] / "release").touch()
            self.wait_for_artwork()
            error.assert_called_once()
        self.assertEqual((old / "old.png").read_bytes(), b"previous")
        self.assertFalse((old / "hero.png").exists())
        self.assertEqual((self.root / "data" / "library.json").read_bytes(), library)
        self.assertEqual(self.app.storage.get_game(self.game["id"])["steamgrid_id"], "")

    @patch("game_manager.ui.artwork_worker", empty_artwork_worker)
    def test_artwork_process_exit_without_result_is_reported_and_cleaned(self):
        self.app.api_key = "test-key"
        with patch("game_manager.ui.messagebox.showerror") as error:
            self.app.search_artwork()
            temporary = self.app.artwork_job["temporary"]
            self.wait_for_artwork()
            error.assert_called_once()
        self.assertFalse(temporary.exists())
        self.assertFalse(self.app.busy)

    def test_artwork_process_start_failure_is_reported_and_cleaned(self):
        self.app.api_key = "test-key"
        before = set(self.app.storage.data_dir.iterdir())
        with patch.object(multiprocessing.process.BaseProcess, "start", side_effect=OSError("start failed")), \
                patch("game_manager.ui.messagebox.showerror") as error:
            self.app.search_artwork()
            error.assert_called_once()
        self.assertIsNone(self.app.artwork_job)
        self.assertEqual(set(self.app.storage.data_dir.iterdir()), before)

    def test_artwork_initialization_failure_is_reported_and_cleaned(self):
        self.app.api_key = "test-key"
        before = set(self.app.storage.data_dir.iterdir())
        for target in ("game_manager.ui.tempfile.mkdtemp", "multiprocessing.context.BaseContext.Pipe"):
            with self.subTest(target=target), patch(target, side_effect=OSError("initialization failed")), \
                    patch("game_manager.ui.messagebox.showerror") as error:
                self.app.search_artwork()
                error.assert_called_once()
                self.assertIsNone(self.app.artwork_job)
                self.assertEqual(set(self.app.storage.data_dir.iterdir()), before)

    @patch("game_manager.ui.artwork_worker", choice_artwork_worker)
    def test_search_waits_for_open_settings_then_shows_game_choices(self):
        from game_manager.ui import GameChoiceDialog
        self.app.api_key = "test-key"
        self.app.search_artwork()
        job = self.app.artwork_job
        self.app.settings()
        settings = self.app.dialog
        self.wait_until(lambda: not job["process"].is_alive())
        self.assertIs(self.app.artwork_job, job)
        self.assertIs(self.app.dialog, settings)
        settings.save()
        self.wait_for_artwork()
        self.assertIsInstance(self.app.dialog, GameChoiceDialog)
        self.assertTrue(self.app.dialog.winfo_exists())
        self.app.dialog.destroy()

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_download_waits_for_edit_form_without_overwriting_game_id(self):
        from game_manager.ui import GameDialog
        job = self.start_held_download()
        self.app.open_dialog(GameDialog, self.game)
        dialog = self.app.dialog
        dialog.entries["chinese_name"].delete(0, "end")
        dialog.entries["chinese_name"].insert(0, "表单修改的名称")
        (job["temporary"] / "release").touch()
        self.wait_until(lambda: not job["process"].is_alive())
        self.assertIs(self.app.artwork_job, job)
        self.assertFalse(self.app.storage.artwork_dir(self.game).exists())
        dialog.save()
        self.wait_for_artwork()
        current = self.app.storage.get_game(self.game["id"])
        self.assertEqual(current["chinese_name"], "表单修改的名称")
        self.assertEqual(current["steamgrid_id"], 42)

    def test_stop_terminates_real_request_waiting_for_http_proxy(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        connected, release = threading.Event(), threading.Event()

        class HoldingProxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                connected.set()
                release.wait(8)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), HoldingProxy)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.app.storage.update_settings({"proxy_enabled": True,
                                              "proxy_url": f"http://127.0.0.1:{server.server_port}"})
            self.app.api_key = "test-key"
            self.app.search_artwork()
            job = self.app.artwork_job
            self.wait_until(connected.is_set)
            self.assertTrue(job["process"].is_alive())
            self.app.stop_artwork()
            self.wait_for_artwork()
            self.assertFalse(job["temporary"].exists())
            self.assertIn("已停止", self.app.status.cget("text"))
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_library_transfer_is_available_without_any_game(self):
        self.app.storage.delete_game(self.game["id"])
        self.app.selected_id = None
        self.app.refresh()
        self.app.library_transfer()
        self.assertTrue(self.app.dialog.winfo_exists())
        self.assertEqual(self.app.library_button.cget("state"), "normal")
        self.app.dialog.destroy()

    def test_whole_library_round_trip_through_ui_restores_saved_settings(self):
        from game_manager.backups import BackupManager
        self.app.backups.create(self.game)
        second_save = self.root / "second-save"
        second_save.mkdir()
        (second_save / "slot.dat").write_text("second")
        second = self.app.storage.save_game({"english_name": "Second Game", "save_path": str(second_save)})
        self.app.backups.create(second)
        settings = {"api_key": "saved-key", "proxy_enabled": True, "proxy_url": "http://127.0.0.1:7890"}
        self.app.storage.update_settings(settings)
        self.app.api_key = "session-only-key"
        self.app.export_library()
        self.wait_for_task()
        package = self.app.library_export_path
        self.assertTrue(package.is_file())
        with zipfile.ZipFile(package) as archive:
            library = json.loads(archive.read("library.json"))
            self.assertEqual(library["settings"], settings)
            self.assertNotIn("session-only-key", archive.read("library.json").decode())
        self.app.storage = Storage(self.root / "new-computer")
        self.app.backups = BackupManager(self.app.storage)
        self.app.selected_id = None
        self.app.refresh()
        (self.save_dir / "slot.dat").write_text("actual save remains current")
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(package)):
            self.app.import_library_archive()
            self.wait_for_task()
        self.assertEqual({item["id"] for item in self.app.storage.games}, {self.game["id"], second["id"]})
        self.assertEqual(self.app.storage.settings, settings)
        self.assertEqual(self.app.api_key, os.environ.get("STEAMGRIDDB_API_KEY") or "saved-key")
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)
        self.assertEqual(len(self.app.backups.list_backups(second)), 1)
        self.assertEqual((self.save_dir / "slot.dat").read_text(), "actual save remains current")
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(package)):
            self.app.import_library_archive()
            self.wait_for_task()
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)
        self.assertIn("跳过 2 条", self.app.status.cget("text"))

    def test_library_import_cancel_or_invalid_package_keeps_existing_data(self):
        self.app.backups.create(self.game)
        self.app.export_library()
        self.wait_for_task()
        library = (self.root / "data" / "library.json").read_bytes()
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(self.app.library_export_path)), \
                patch("game_manager.ui.messagebox.askyesno", return_value=False):
            self.app.import_library_archive()
            self.wait_for_task()
        self.assertEqual((self.root / "data" / "library.json").read_bytes(), library)
        self.assertIn("已取消", self.app.status.cget("text"))
        invalid = self.root / "invalid-library.zip"
        invalid.write_bytes(b"invalid zip")
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(invalid)), \
                patch("game_manager.ui.messagebox.showerror") as error:
            self.app.import_library_archive()
            self.wait_for_task()
            error.assert_called_once()
        self.assertEqual((self.root / "data" / "library.json").read_bytes(), library)
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)

    def test_library_merge_preserves_local_game_fields_and_selection(self):
        self.app.backups.create(self.game)
        self.app.storage.update_settings({"api_key": "source-saved-key"})
        self.app.export_library()
        self.wait_for_task()
        current = self.app.storage.get_game(self.game["id"])
        current["chinese_name"] = "目标电脑名称"
        current["save_path"] = str(self.root / "target-computer-save")
        self.app.storage.save_game(current)
        local_only = self.app.storage.save_game({"english_name": "Local Only Game"})
        self.app.storage.update_settings({"api_key": "target-key", "target_only": "keep"})
        self.app.select(local_only["id"])
        with patch("game_manager.ui.filedialog.askopenfilename", return_value=str(self.app.library_export_path)):
            self.app.import_library_archive()
            self.wait_for_task()
        self.assertEqual(self.app.selected_id, local_only["id"])
        merged = self.app.storage.get_game(self.game["id"])
        self.assertEqual(merged["save_path"], current["save_path"])
        self.assertEqual(merged["chinese_name"], current["chinese_name"])
        self.assertEqual(self.app.storage.settings["api_key"], "source-saved-key")
        self.assertEqual(self.app.storage.settings["target_only"], "keep")
        self.assertEqual(len(self.app.backups.list_backups(self.game)), 1)

    @patch("game_manager.ui.artwork_worker", held_artwork_worker)
    def test_library_export_during_artwork_fetch_excludes_staging_and_keeps_stop_available(self):
        job = self.start_held_download()
        self.app.export_library()
        self.wait_for_task()
        self.assertIs(self.app.artwork_job, job)
        self.assertEqual(self.app.stop_artwork_button.cget("state"), "normal")
        with zipfile.ZipFile(self.app.library_export_path) as archive:
            names = archive.namelist()
            self.assertIn("library.json", names)
            self.assertFalse(any(".artwork-" in name or name.endswith("hero.png") for name in names))
        self.app.stop_artwork()
        self.wait_for_artwork()
        self.assertFalse(job["temporary"].exists())


if __name__ == "__main__":
    unittest.main()
