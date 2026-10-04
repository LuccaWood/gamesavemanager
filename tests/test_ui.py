"""真实 Tk 集成测试；需要桌面会话，设置 GAME_MANAGER_GUI_TESTS=1 启用。"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from game_manager.storage import Storage


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
                        patch("game_manager.ui.messagebox.showerror")]
        for item in self.patches:
            item.start()
        self.app = GameManagerApp(self.root / "data")
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
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
        self.assertEqual(len(self.app.backup_table.get_children()), 3)
        self.app.tabs.set("数据打包")
        self.app.export_game()
        self.wait_for_task()
        self.assertEqual(self.app.tabs.get(), "数据打包")
        export = self.app.export_paths[self.game["id"]]
        self.assertTrue(export.is_file())
        with zipfile.ZipFile(export) as archive:
            self.assertIn("game.json", archive.namelist())
            self.assertEqual(sum(name.endswith("save.zip") for name in archive.namelist()), 3)
        self.app.backup_table.selection_set(original_id)
        self.app.delete_backup()
        self.wait_for_task()
        self.assertEqual(len(self.app.backup_table.get_children()), 2)

    def test_busy_disables_switching_and_controls(self):
        second = self.app.storage.save_game({"english_name": "Second Game"})
        self.app.refresh()
        self.app.run_task("等待测试任务", lambda: time.sleep(0.15), lambda _: None)
        self.app.select(second["id"])
        self.assertEqual(self.app.selected_id, self.game["id"])
        self.assertEqual(self.app.add_button.cget("state"), "disabled")
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


if __name__ == "__main__":
    unittest.main()
