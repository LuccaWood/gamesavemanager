from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest import mock

from game_manager.backups import BackupManager
from game_manager.storage import Storage, atomic_write_json


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.save = self.root / "游戏存档"
        self.save.mkdir()
        self.storage = Storage(self.root / "data")
        self.game = self.storage.save_game({"english_name": " Test Game ", "chinese_name": "测试游戏",
                                            "game_path": "", "save_path": str(self.save)})
        self.manager = BackupManager(self.storage)

    def write(self, name: str, contents: str):
        path = self.save / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def test_storage_persistence_edit_delete_keeps_resources(self):
        self.storage.update_settings({"steamgriddb_api_key": "secret"})
        directory = self.storage.game_dir(self.game)
        directory.mkdir(parents=True)
        (directory / "keep").write_text("saved")
        changed = self.storage.save_game({**self.game, "chinese_name": "修改后"})
        loaded = Storage(self.storage.data_dir)
        self.assertEqual(loaded.get_game(changed["id"])["chinese_name"], "修改后")
        self.assertEqual(loaded.settings["steamgriddb_api_key"], "secret")
        self.assertEqual(changed["english_name"], "Test Game")
        with self.assertRaises(ValueError):
            loaded.save_game({"english_name": " test GAME "})
        loaded.delete_game(changed["id"])
        self.assertTrue((directory / "keep").is_file())
        self.assertEqual(Storage(self.storage.data_dir).games, [])

    def test_corrupt_json_is_not_overwritten(self):
        path = self.storage.data_dir / "library.json"
        path.write_text("broken")
        with self.assertRaisesRegex(ValueError, "数据文件损坏"):
            Storage(self.storage.data_dir)
        self.assertEqual(path.read_text(), "broken")

    def test_atomic_json_failure_keeps_previous_file(self):
        path = self.root / "state.json"
        atomic_write_json(path, {"old": True})
        with mock.patch("game_manager.storage.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"new": True})
        self.assertEqual(json.loads(path.read_text()), {"old": True})
        self.assertEqual(list(self.root.glob(".state.json.*.tmp")), [])

    def test_backup_restore_completely_replaces_directory_and_saves_current(self):
        self.write("nested/中文.sav", "old")
        (self.save / "empty").mkdir()
        backup = self.manager.create(self.game)
        timestamp = datetime.fromisoformat(backup["created_at"])
        self.assertIsNotNone(timestamp.utcoffset())
        self.assertEqual(timestamp.microsecond, 0)
        self.write("nested/中文.sav", "new")
        self.write("extra.sav", "remove me")
        safety = self.manager.restore(self.game, backup["id"])
        self.assertEqual((self.save / "nested/中文.sav").read_text(), "old")
        self.assertFalse((self.save / "extra.sav").exists())
        self.assertTrue((self.save / "empty").is_dir())
        self.assertEqual(safety["reason"], "before_restore")
        with zipfile.ZipFile(self.manager.backup_dir(self.game, safety["id"]) / "save.zip") as archive:
            self.assertEqual(archive.read("extra.sav"), b"remove me")

    def test_empty_save_and_missing_destination_restore(self):
        backup = self.manager.create(self.game)
        self.save.rmdir()
        self.assertIsNone(self.manager.restore(self.game, backup["id"]))
        self.assertTrue(self.save.is_dir())
        self.assertEqual(list(self.save.iterdir()), [])

    def test_copy_delete_sequence_persists_without_reuse(self):
        self.write("slot.sav", "payload")
        first = self.manager.create(self.game)
        copied = self.manager.copy(self.game, first["id"])
        self.assertEqual(copied["sequence"], 2)
        self.assertNotEqual(first["id"], copied["id"])
        self.assertEqual((self.manager.backup_dir(self.game, first["id"]) / "save.zip").read_bytes(),
                         (self.manager.backup_dir(self.game, copied["id"]) / "save.zip").read_bytes())
        self.manager.delete(self.game, copied["id"])
        self.manager.delete(self.game, first["id"])
        reloaded = BackupManager(Storage(self.storage.data_dir))
        next_backup = reloaded.create(self.game)
        self.assertEqual(next_backup["sequence"], 3)
        self.assertEqual([entry["sequence"] for entry in reloaded.list_backups(self.game)], [3])

    def test_same_second_backups_have_distinct_ids(self):
        fixed = datetime.now().astimezone().replace(microsecond=0)
        with mock.patch("game_manager.backups.datetime", wraps=datetime) as clock:
            clock.now.return_value = fixed
            first = self.manager.create(self.game)
            second = self.manager.create(self.game)
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([entry["sequence"] for entry in self.manager.list_backups(self.game)], [2, 1])

    def test_dangerous_targets_and_overlap_are_rejected(self):
        for path in (Path(self.root.anchor), Path.home(), self.storage.data_dir,
                     self.storage.data_dir / "nested", self.root, self.manager.application_dir):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.manager.create({**self.game, "save_path": str(path)})

    def test_application_subdirectory_save_is_allowed_but_data_stays_protected(self):
        self.manager.application_dir = self.root
        self.write("slot.sav", "old")
        backup = self.manager.create(self.game)
        self.write("slot.sav", "current")
        self.manager.restore(self.game, backup["id"])
        self.assertEqual((self.save / "slot.sav").read_text(), "old")
        for path in (self.root, self.root.parent):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "应用目录"):
                self.manager.create({**self.game, "save_path": str(path)})
        for path in (self.storage.data_dir, self.storage.data_dir / "nested"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "数据目录"):
                self.manager.create({**self.game, "save_path": str(path)})

    def test_path_variables_are_expanded_only_during_operation(self):
        for raw in ("$GSM_TEST_SAVE", "%GSM_TEST_SAVE%"):
            with self.subTest(path=raw), mock.patch.dict(os.environ, {"GSM_TEST_SAVE": str(self.save)}):
                changed = self.storage.save_game({**self.game, "save_path": raw})
                self.assertEqual(changed["save_path"], raw)
                self.assertGreater(self.manager.create(changed)["size"], 0)

    def test_source_symlink_is_rejected(self):
        outside = self.root / "outside.sav"
        outside.write_text("private")
        try:
            (self.save / "linked.sav").symlink_to(outside)
        except OSError:
            self.skipTest("当前系统不允许创建符号链接")
        with self.assertRaises(ValueError):
            self.manager.create(self.game)
        self.assertEqual(self.manager.list_backups(self.game), [])

    def test_backup_walk_error_does_not_publish_partial_archive(self):
        self.write("slot.sav", "current")
        self.write("blocked/nested.sav", "protected")
        existing = self.manager.create(self.game)
        existing_dir = self.manager.backup_dir(self.game, existing["id"])
        existing_bytes = (existing_dir / "save.zip").read_bytes()

        def failed_walk(top, *, followlinks, onerror):
            yield str(top), ["blocked"], ["slot.sav"]
            onerror(PermissionError("simulated unreadable save directory"))

        with mock.patch("game_manager.backups.os.walk", side_effect=failed_walk):
            with self.assertRaises(PermissionError):
                self.manager.create(self.game)
        self.assertEqual(set(existing_dir.parent.iterdir()), {existing_dir})
        self.assertEqual((existing_dir / "save.zip").read_bytes(), existing_bytes)
        self.assertEqual((self.save / "slot.sav").read_text(), "current")
        self.assertEqual((self.save / "blocked/nested.sav").read_text(), "protected")

    def test_data_directory_symlink_is_rejected_before_writing(self):
        outside = self.root / "outside-data"
        outside.mkdir()
        games = self.storage.data_dir / "games"
        try:
            games.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("当前系统不允许创建符号链接")
        with self.assertRaises(ValueError):
            self.manager.create(self.game)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unsafe_zip_leaves_current_content_untouched(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        archive_path = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        cases = [["../outside"], ["/absolute"], ["C:/outside"], ["folder\\outside"], ["NUL.txt"],
                 ["bad. /file"], ["slot", "SLOT"], ["A/one", "a/two"], ["file", "file/child"],
                 ["same", "same/"]]
        for names in cases:
            with self.subTest(names=names):
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name in names:
                        archive.writestr(name, b"malicious")
                with self.assertRaises(ValueError):
                    self.manager.restore(self.game, backup["id"])
                self.assertEqual((self.save / "slot.sav").read_text(), "current")
                self.assertFalse((self.root / "outside").exists())
        with zipfile.ZipFile(archive_path, "w") as archive:
            link = zipfile.ZipInfo("symlink")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "../outside")
        with self.assertRaises(ValueError):
            self.manager.restore(self.game, backup["id"])
        self.assertEqual((self.save / "slot.sav").read_text(), "current")

    def test_corrupt_zip_preserves_current_directory(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        archive_path = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        archive_path.write_bytes(b"broken zip")
        with self.assertRaises(zipfile.BadZipFile):
            self.manager.restore(self.game, backup["id"])
        self.assertEqual((self.save / "slot.sav").read_text(), "current")
        self.assertEqual(list(self.root.glob(".gsm-*")), [])

    def test_crc_failure_preserves_current_directory(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        archive_path = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("slot.sav", b"payload-unique")
        data = archive_path.read_bytes().replace(b"payload-unique", b"damaged-unique")
        archive_path.write_bytes(data)
        with self.assertRaises(zipfile.BadZipFile):
            self.manager.restore(self.game, backup["id"])
        self.assertEqual((self.save / "slot.sav").read_text(), "current")

    def test_failed_directory_swap_rolls_back(self):
        self.write("slot.sav", "old")
        backup = self.manager.create(self.game)
        self.write("slot.sav", "current")
        original_rename = Path.rename

        def fail_install(path, target):
            if path.name.startswith(".gsm-restore-"):
                raise PermissionError("simulated locked directory")
            return original_rename(path, target)

        with mock.patch.object(Path, "rename", fail_install):
            with self.assertRaises(PermissionError):
                self.manager.restore(self.game, backup["id"])
        self.assertEqual((self.save / "slot.sav").read_text(), "current")
        self.assertEqual(list(self.root.glob(".gsm-*")), [])
        self.assertEqual(self.manager.list_backups(self.game)[0]["reason"], "before_restore")

    def test_export_contains_backups_artwork_and_no_settings(self):
        self.write("slot.sav", "game data")
        backup = self.manager.create(self.game)
        self.storage.update_settings({"steamgriddb_api_key": "DO_NOT_EXPORT"})
        artwork = self.storage.artwork_dir(self.game)
        artwork.mkdir()
        (artwork / "cover.png").write_bytes(b"image")
        atomic_write_json(artwork / "sources.json", {"cover": "https://example.test/art"})
        output = self.manager.export({**self.game, "api_key": "DO_NOT_EXPORT", "settings": self.storage.settings})
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, {"game.json", f"backups/{backup['id']}/save.zip",
                                    f"backups/{backup['id']}/metadata.json", "artwork/cover.png", "artwork/sources.json"})
            self.assertNotIn(b"DO_NOT_EXPORT", archive.read("game.json"))
            self.assertEqual(json.loads(archive.read("game.json"))["id"], self.game["id"])
        self.assertEqual(output.parent, self.storage.data_dir / "exports")

    def test_export_rejects_damaged_and_unsafe_backups_without_publishing(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        previous_export = self.manager.export(self.game)
        archive_path = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        for case in ("invalid", "crc", "unsafe"):
            with self.subTest(case=case):
                if case == "invalid":
                    archive_path.write_bytes(b"broken zip")
                else:
                    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                        archive.writestr("../outside" if case == "unsafe" else "slot.sav", b"payload-unique")
                    if case == "crc":
                        archive_path.write_bytes(archive_path.read_bytes().replace(b"payload-unique", b"damaged-unique"))
                with self.assertRaisesRegex(ValueError, "备份 #000001 校验失败，无法导出"):
                    self.manager.export(self.game)
                self.assertEqual(set(previous_export.parent.iterdir()), {previous_export})
                self.assertEqual((self.save / "slot.sav").read_text(), "current")

    def test_export_walk_error_does_not_publish_partial_archive(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        artwork = self.storage.artwork_dir(self.game)
        (artwork / "blocked").mkdir(parents=True)
        (artwork / "cover.png").write_bytes(b"cover")
        (artwork / "blocked/hero.png").write_bytes(b"hero")
        previous_export = self.manager.export(self.game)
        previous_bytes = previous_export.read_bytes()
        backup_path = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        backup_bytes = backup_path.read_bytes()

        def failed_walk(top, *, followlinks, onerror):
            yield str(top), ["blocked"], ["cover.png"]
            onerror(PermissionError("simulated unreadable artwork directory"))

        with mock.patch("game_manager.backups.os.walk", side_effect=failed_walk):
            with self.assertRaises(PermissionError):
                self.manager.export(self.game)
        self.assertEqual(set(previous_export.parent.iterdir()), {previous_export})
        self.assertEqual(previous_export.read_bytes(), previous_bytes)
        self.assertEqual(backup_path.read_bytes(), backup_bytes)
        self.assertEqual((artwork / "blocked/hero.png").read_bytes(), b"hero")
        self.assertEqual((self.save / "slot.sav").read_text(), "current")

    def test_export_filename_is_recognizable_and_windows_compatible(self):
        game = {**self.game, "english_name": "Test Game: 中文/\\?*" + "X" * 70}
        output = self.manager.export(game)
        prefix, game_id, date, time, suffix = output.stem.rsplit("_", 4)
        self.assertTrue(prefix.startswith("Test_Game_"))
        self.assertEqual(len(prefix), 60)
        self.assertRegex(prefix, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(game_id, self.game["id"])
        self.assertRegex(date, r"^\d{8}$")
        self.assertRegex(time, r"^\d{6}$")
        self.assertRegex(suffix, r"^[0-9a-f]{8}$")
        self.assertEqual(output.suffix, ".zip")
        self.assertLess(len(output.name), 255)


if __name__ == "__main__":
    unittest.main()
