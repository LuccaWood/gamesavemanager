"""真实 Tk 集成测试；需要桌面会话，设置 GAME_MANAGER_GUI_TESTS=1 启用。"""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
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


@unittest.skipUnless(os.environ.get("GAME_MANAGER_GUI_TESTS") == "1", "需要可用桌面会话")
class UITests(unittest.TestCase):
    def setUp(self):
        from game_manager.ui import GameManagerApp
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
        if getattr(self.app, "artwork_job", None):
            self.app.stop_artwork()
            self.wait_for_artwork()
        self.app.destroy()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

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
        self.assertEqual(settings.proxy_enabled.cget("text"), "为软件联网请求使用 HTTP 代理")
        settings.destroy()

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
        settings.proxy_url.insert(0, "socks5://127.0.0.1:1080")
        previous = (self.root / "data" / "library.json").read_bytes()
        with patch("game_manager.ui.messagebox.showerror") as error:
            settings.save()
            error.assert_called_once()
        self.assertTrue(settings.winfo_exists())
        self.assertEqual((self.root / "data" / "library.json").read_bytes(), previous)
        settings.destroy()

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
