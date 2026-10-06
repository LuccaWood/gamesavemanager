from __future__ import annotations

import json
import ntpath
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

    def test_windows_generated_members_backup_restore_and_export(self):
        self.write("76561197969271872/SAVEDATA00/slot.sav", "old")
        (self.save / "76561197969271872/SAVEDATA00/empty").mkdir()
        original = zipfile.ZipInfo.from_file

        def windows_from_file(filename, arcname=None, **kwargs):
            arcname = ntpath.normpath(os.fspath(arcname if arcname is not None else filename))
            with mock.patch.object(zipfile.os, "sep", "\\"):
                return original(filename, arcname, **kwargs)

        game = {**self.game, "save_path": "%LOCALAPPDATA%/游戏存档"}
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            with mock.patch.object(zipfile.ZipInfo, "from_file", side_effect=windows_from_file):
                backup = self.manager.create(game)
                package = self.manager.export(game)
                self.write("76561197969271872/SAVEDATA00/slot.sav", "current")
                self.manager.restore(game, backup["id"])
        self.assertEqual((self.save / "76561197969271872/SAVEDATA00/slot.sav").read_text(), "old")
        self.assertTrue((self.save / "76561197969271872/SAVEDATA00/empty").is_dir())
        with zipfile.ZipFile(package) as archive:
            self.assertIn(f"backups/{backup['id']}/save.zip", archive.namelist())

    def test_nul_zip_name_is_rejected_before_restore_or_export(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        archive_path = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("valid.sav", b"payload")
        archive_path.write_bytes(archive_path.read_bytes().replace(b"valid.sav", b"valid\x00sav"))
        for operation in (lambda: self.manager.restore(self.game, backup["id"]), lambda: self.manager.export(self.game)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual((self.save / "slot.sav").read_text(), "current")

    def test_read_zip_rejects_original_backslash_even_when_windows_normalizes_it(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        archive_path = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("nested/slot.sav", b"payload")
        archive_path.write_bytes(archive_path.read_bytes().replace(b"nested/slot.sav", b"nested\\slot.sav"))
        sanitize = zipfile._sanitize_filename
        with mock.patch.object(zipfile, "_sanitize_filename", side_effect=lambda name: sanitize(name.replace("\\", "/"))):
            with self.assertRaises(ValueError):
                self.manager.restore(self.game, backup["id"])
        self.assertEqual((self.save / "slot.sav").read_text(), "current")

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

    def recipient(self):
        storage = Storage(self.root / "recipient")
        return storage, BackupManager(storage)

    def rewrite_package(self, package, updates=None, omitted=()):
        updates = updates or {}
        path = self.root / "edited.zip"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
            names = set()
            for member in source.infolist():
                if member.filename not in omitted:
                    output.writestr(member, updates.get(member.filename, source.read(member)))
                    names.add(member.filename)
            for name, data in updates.items():
                if name not in names:
                    output.writestr(name, data)
        return path

    def test_empty_library_import_round_trip_and_repeat_preserves_copies(self):
        self.write("nested/slot.sav", "old")
        (self.save / "empty").mkdir()
        first = self.manager.create(self.game)
        self.manager.copy(self.game, first["id"])
        artwork = self.storage.artwork_dir(self.game)
        artwork.mkdir()
        (artwork / "cover.png").write_bytes(b"cover")
        atomic_write_json(artwork / "sources.json", {"url": "https://example.test/cover"})
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        preview = manager.inspect_import(package)
        self.assertEqual(preview["backup_count"], 2)
        self.assertTrue(preview["has_artwork"])
        self.assertIsNone(preview["existing_game"])
        with mock.patch.object(manager, "_save_path", side_effect=AssertionError("import accessed live saves")):
            result = manager.import_game(package)
            repeated = manager.import_game(package)
        self.assertEqual(result["game"], self.game)
        self.assertEqual((result["imported"], repeated["imported"], repeated["skipped"]), (2, 0, 2))
        self.assertEqual(len(manager.list_backups(result["game"])), 2)
        self.assertEqual((storage.artwork_dir(result["game"]) / "cover.png").read_bytes(), b"cover")
        self.write("nested/slot.sav", "current")
        self.write("extra.sav", "remove")
        manager.restore(result["game"], first["id"])
        self.assertEqual((self.save / "nested/slot.sav").read_text(), "old")
        self.assertFalse((self.save / "extra.sav").exists())
        self.assertTrue((self.save / "empty").is_dir())

    def test_import_matches_name_keeps_local_config_and_renumbers_conflict(self):
        self.write("slot.sav", "source")
        source_backup = self.manager.create(self.game)
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        local = storage.save_game({"english_name": "test game", "chinese_name": "保留本机名称",
                                   "save_path": str(self.save), "game_path": "%LOCAL_GAME%", "steamgrid_id": 42})
        self.write("slot.sav", "local")
        local_backup = manager.create(local)
        local_zip = manager.backup_dir(local, local_backup["id"]) / "save.zip"
        original_bytes = local_zip.read_bytes()
        self.assertEqual(manager.inspect_import(package)["existing_game"], local)
        result = manager.import_game(package)
        self.assertEqual(result["game"], local)
        self.assertEqual(result["imported"], 1)
        records = manager.list_backups(local)
        self.assertEqual([record["sequence"] for record in records], [2, 1])
        self.assertEqual(records[0]["created_at"], source_backup["created_at"])
        self.assertEqual(local_zip.read_bytes(), original_bytes)
        repeat = manager.import_game(package)
        self.assertEqual((repeat["imported"], repeat["skipped"]), (0, 1))
        self.assertEqual(manager.create(local)["sequence"], 3)

    def test_import_reuses_deleted_game_resources_and_preserves_other_data(self):
        self.write("slot.sav", "saved")
        first = self.manager.create(self.game)
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        manager.import_game(package)
        previous_export = manager.export(self.game)
        other = storage.save_game({"english_name": "Other Game"})
        other_dir = storage.game_dir(other)
        other_dir.mkdir()
        (other_dir / "keep").write_text("other")
        storage.delete_game(self.game["id"])
        self.manager.copy(self.game, first["id"])
        result = manager.import_game(self.manager.export(self.game))
        self.assertEqual((result["imported"], result["skipped"]), (1, 1))
        self.assertEqual(result["game"]["id"], self.game["id"])
        self.assertEqual(manager.create(result["game"])["sequence"], 3)
        self.assertTrue(previous_export.is_file())
        self.assertEqual((other_dir / "keep").read_text(), "other")
        self.assertEqual(storage.get_game(other["id"]), other)

    def test_empty_package_import_and_identity_conflict(self):
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        result = manager.import_game(package)
        self.assertEqual((result["imported"], result["skipped"]), (0, 0))
        self.assertEqual(manager.list_backups(result["game"]), [])
        self.assertEqual(manager.create(result["game"])["sequence"], 1)
        storage.save_game({**result["game"], "english_name": "Renamed"})
        storage.save_game({"english_name": self.game["english_name"]})
        before = (storage.data_dir / "library.json").read_bytes()
        for operation in (manager.inspect_import, manager.import_game):
            with self.assertRaisesRegex(ValueError, "分别对应不同游戏"):
                operation(package)
        self.assertEqual((storage.data_dir / "library.json").read_bytes(), before)

    def test_invalid_import_packages_leave_existing_game_untouched(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        manager.import_game(package)
        directory = storage.game_dir(self.game)
        before = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        library = (storage.data_dir / "library.json").read_bytes()
        metadata = json.loads((self.manager.backup_dir(self.game, backup["id"]) / "metadata.json").read_text())
        prefix = f"backups/{backup['id']}/"
        cases = [({"../escape": b"unsafe"}, ()), ({"settings.json": b"{}"}, ()),
                 ({}, (prefix + "save.zip",)), ({prefix + "save.zip": b"broken"}, ())]
        invalid_metadata = {**metadata, "created_at": "2026-10-06T12:00:00"}
        cases.append(({prefix + "metadata.json": json.dumps(invalid_metadata).encode()}, ()))
        for updates, omitted in cases:
            with self.subTest(updates=list(updates), omitted=omitted):
                invalid = self.rewrite_package(package, updates, omitted)
                with self.assertRaises(ValueError):
                    manager.import_game(invalid)
                self.assertEqual((storage.data_dir / "library.json").read_bytes(), library)
                self.assertEqual({path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}, before)
                self.assertEqual(set(directory.parent.iterdir()), {directory})
        nul = self.rewrite_package(package)
        nul.write_bytes(nul.read_bytes().replace(b"game.json", b"game\x00json"))
        with self.assertRaises(ValueError):
            manager.import_game(nul)
        self.assertEqual((self.save / "slot.sav").read_text(), "current")

    def test_import_checks_nested_crc_and_paths(self):
        self.write("slot.sav", "current")
        backup = self.manager.create(self.game)
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        prefix = f"backups/{backup['id']}/"
        metadata = json.loads((self.manager.backup_dir(self.game, backup["id"]) / "metadata.json").read_text())
        inner = self.root / "inner.zip"
        for filename in ("../outside", "valid.sav", "crc.sav"):
            with zipfile.ZipFile(inner, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(filename, b"payload-unique")
            data = inner.read_bytes()
            if filename == "valid.sav":
                data = data.replace(b"valid.sav", b"valid\x00sav")
            elif filename == "crc.sav":
                data = data.replace(b"payload-unique", b"damaged-unique")
            invalid = self.rewrite_package(package, {prefix + "save.zip": data,
                                                     prefix + "metadata.json": json.dumps({**metadata, "size": len(data)}).encode()})
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                manager.import_game(invalid)
            self.assertEqual(storage.games, [])
            self.assertFalse((storage.data_dir / "games").exists())

    def test_import_library_write_failure_rolls_back_directory_and_memory(self):
        self.write("slot.sav", "first")
        self.manager.create(self.game)
        storage, manager = self.recipient()
        manager.import_game(self.manager.export(self.game))
        directory = storage.game_dir(self.game)
        before = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        library = (storage.data_dir / "library.json").read_bytes()
        original_game = storage.get_game(self.game["id"])
        self.write("slot.sav", "second")
        self.manager.create(self.game)
        package = self.manager.export(self.game)
        original_replace = os.replace

        def fail_library(source, destination):
            if Path(destination) == storage.data_dir / "library.json":
                storage.games[0]["chinese_name"] = "simulated partial mutation"
                raise PermissionError("simulated configuration write failure")
            return original_replace(source, destination)

        with mock.patch("game_manager.storage.os.replace", side_effect=fail_library):
            with self.assertRaises(PermissionError):
                manager.import_game(package)
        self.assertEqual(storage.get_game(self.game["id"]), original_game)
        self.assertEqual((storage.data_dir / "library.json").read_bytes(), library)
        self.assertEqual({path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}, before)
        self.assertEqual(set(directory.parent.iterdir()), {directory})

    def test_import_outer_crc_failure_does_not_publish_resources(self):
        artwork = self.storage.artwork_dir(self.game)
        artwork.mkdir(parents=True)
        (artwork / "cover.png").write_bytes(b"unique_image")
        package = self.manager.export(self.game)
        path = self.root / "corrupt-outer.zip"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
            for name in source.namelist():
                output.writestr(name, source.read(name))
        path.write_bytes(path.read_bytes().replace(b"unique_image", b"broken_image"))
        storage, manager = self.recipient()
        self.assertTrue(manager.inspect_import(path)["has_artwork"])
        with self.assertRaises(ValueError):
            manager.import_game(path)
        self.assertEqual(storage.games, [])
        self.assertFalse((storage.data_dir / "games").exists())

    def test_import_directory_publish_failure_preserves_original(self):
        self.write("slot.sav", "saved")
        self.manager.create(self.game)
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        manager.import_game(package)
        directory = storage.game_dir(self.game)
        before = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        original_rename = Path.rename

        def fail_publish(path, target):
            if path.name == "merged":
                raise PermissionError("simulated import directory lock")
            return original_rename(path, target)

        with mock.patch.object(Path, "rename", fail_publish):
            with self.assertRaises(PermissionError):
                manager.import_game(package)
        self.assertEqual({path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}, before)
        self.assertEqual(set(directory.parent.iterdir()), {directory})

    def test_import_rollback_failure_reports_preserved_original_directory(self):
        self.write("slot.sav", "saved")
        self.manager.create(self.game)
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        manager.import_game(package)
        directory = storage.game_dir(self.game)
        before = {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        original_rename = Path.rename

        def fail_publish_and_rollback(path, target):
            if path.name == "merged" or path.name.startswith(".previous-"):
                raise PermissionError("simulated directory lock")
            return original_rename(path, target)

        with mock.patch.object(Path, "rename", fail_publish_and_rollback):
            with self.assertRaisesRegex(RuntimeError, "保留的游戏资源位于") as caught:
                manager.import_game(package)
        previous = next(directory.parent.glob(".previous-*"))
        self.assertIn(str(previous), str(caught.exception))
        self.assertEqual({path.relative_to(previous): path.read_bytes() for path in previous.rglob("*") if path.is_file()}, before)
        self.assertEqual(storage.get_game(self.game["id"]), self.game)

    def test_new_game_import_configuration_failure_leaves_no_game_or_resources(self):
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        with mock.patch.object(storage, "save_game", side_effect=PermissionError("simulated write failure")):
            with self.assertRaises(PermissionError):
                manager.import_game(package)
        self.assertEqual(storage.games, [])
        self.assertFalse((storage.data_dir / "games").exists())

    def test_import_merges_artwork_files_and_manifest(self):
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        manager.import_game(package)
        local = storage.artwork_dir(self.game)
        local.mkdir()
        (local / "cover.png").write_bytes(b"old cover")
        (local / "logo.png").write_bytes(b"local logo")
        atomic_write_json(local / "assets.json", {"assets": {"cover": {"file": "cover.png", "source_url": "old"},
                                                             "logo": {"file": "logo.png"}}, "missing": ["hero", "wide"]})
        source = self.storage.artwork_dir(self.game)
        (source / "legacy").mkdir(parents=True)
        (source / "cover.png").write_bytes(b"new cover")
        (source / "hero.png").write_bytes(b"new hero")
        (source / "legacy/sources.json").write_text('{"source": "legacy"}')
        atomic_write_json(source / "assets.json", {"assets": {"cover": {"file": "cover.png", "source_url": "new"},
                                                              "hero": {"file": "hero.png"}}, "missing": ["logo", "wide"]})
        manager.import_game(self.manager.export(self.game))
        manifest = json.loads((local / "assets.json").read_text())
        self.assertEqual(set(manifest["assets"]), {"cover", "hero", "logo"})
        self.assertEqual(manifest["assets"]["cover"]["source_url"], "new")
        self.assertEqual(manifest["missing"], ["wide"])
        self.assertEqual((local / "cover.png").read_bytes(), b"new cover")
        self.assertEqual((local / "logo.png").read_bytes(), b"local logo")
        self.assertTrue((local / "legacy/sources.json").is_file())

    def test_import_rejects_local_symlink_without_following_it(self):
        package = self.manager.export(self.game)
        storage, manager = self.recipient()
        manager.import_game(package)
        outside = self.root / "outside-resource"
        outside.write_text("untouched")
        link = storage.game_dir(self.game) / "linked"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("当前系统不允许创建符号链接")
        with self.assertRaises(ValueError):
            manager.import_game(package)
        self.assertEqual(outside.read_text(), "untouched")
        self.assertTrue(link.is_symlink())

    def test_import_bare_image_does_not_keep_stale_source_metadata(self):
        storage, manager = self.recipient()
        manager.import_game(self.manager.export(self.game))
        local = storage.artwork_dir(self.game)
        local.mkdir()
        (local / "cover.png").write_bytes(b"old")
        atomic_write_json(local / "assets.json", {"assets": {"cover": {"file": "cover.png", "source_url": "old"}}, "missing": []})
        source = self.storage.artwork_dir(self.game)
        source.mkdir(parents=True)
        (source / "cover.png").write_bytes(b"new")
        manager.import_game(self.manager.export(self.game))
        self.assertEqual((local / "cover.png").read_bytes(), b"new")
        self.assertNotIn("cover", json.loads((local / "assets.json").read_text())["assets"])

    def test_import_artwork_case_conflict_does_not_publish_unexportable_tree(self):
        storage, manager = self.recipient()
        manager.import_game(self.manager.export(self.game))
        local = storage.artwork_dir(self.game)
        local.mkdir()
        (local / "cover.png").write_bytes(b"old")
        source = self.storage.artwork_dir(self.game)
        source.mkdir(parents=True)
        (source / "Cover.png").write_bytes(b"new")
        package = self.manager.export(self.game)
        with self.assertRaisesRegex(ValueError, "大小写冲突"):
            manager.import_game(package)
        self.assertEqual((local / "cover.png").read_bytes(), b"old")
        self.assertEqual({path.name for path in local.iterdir()}, {"cover.png"})
        self.assertTrue(manager.export(self.game).is_file())

    def test_library_roundtrip_preserves_all_resources_settings_and_counters(self):
        self.write("slot.sav", "first")
        first = self.manager.create(self.game)
        second = self.storage.save_game({"english_name": "Other Game", "save_path": "%LOCALAPPDATA%/other"})
        artwork = self.storage.artwork_dir(second)
        artwork.mkdir(parents=True)
        (artwork / "cover.png").write_bytes(b"cover")
        orphan = self.storage.save_game({"english_name": "Deleted Game", "save_path": str(self.save)})
        self.manager.create(orphan)
        self.storage.delete_game(orphan["id"])
        atomic_write_json(self.storage.game_dir(self.game) / "sequence.json", {"next_sequence": 100})
        self.storage.update_settings({"api_key": "saved-key", "http_proxy": "http://127.0.0.1:7890"})
        (self.storage.data_dir / ".artwork-job").mkdir()
        (self.storage.data_dir / "exports").mkdir()
        (self.storage.data_dir / "exports/old.zip").write_bytes(b"do not include")
        package = self.manager.export_library()
        self.assertTrue(package.name.startswith("Library_"))
        with zipfile.ZipFile(package) as archive:
            self.assertTrue(all(name == "library.json" or name.startswith("games/") for name in archive.namelist()))
            self.assertIn(f"games/{orphan['id']}/sequence.json", archive.namelist())
        storage, manager = self.recipient()
        preview = manager.inspect_library(package)
        self.assertEqual(preview, {"game_count": 2, "backup_count": 2, "existing_count": 0,
                                   "new_count": 2, "orphan_count": 1, "settings_count": 2})
        with mock.patch.object(manager, "_save_path", side_effect=AssertionError("真实存档不应被访问")):
            result = manager.import_library(package)
        self.assertEqual((result["new_games"], result["merged_games"], result["imported"], result["skipped"]), (2, 0, 2, 0))
        self.assertEqual(result["orphan_count"], 1)
        self.assertEqual(set(result["settings_keys"]), {"api_key", "http_proxy"})
        self.assertEqual(Storage(storage.data_dir).settings, self.storage.settings)
        self.assertEqual(storage.get_game(second["id"])["save_path"], "%LOCALAPPDATA%/other")
        self.assertEqual((storage.artwork_dir(second) / "cover.png").read_bytes(), b"cover")
        self.assertTrue(storage.game_dir(orphan).is_dir())
        again = manager.import_library(package)
        self.assertEqual((again["imported"], again["skipped"]), (0, 2))
        self.assertEqual(manager.create(storage.get_game(self.game["id"]))["sequence"], 100)
        self.write("slot.sav", "changed")
        manager.restore(storage.get_game(self.game["id"]), first["id"])
        self.assertEqual((self.save / "slot.sav").read_text(), "first")

    def test_library_damaged_second_game_does_not_publish_first_game(self):
        self.write("slot.sav", "first")
        self.manager.create(self.game)
        second = self.storage.save_game({"english_name": "Second Game", "save_path": str(self.save)})
        backup = self.manager.create(second)
        package = self.manager.export_library()
        invalid = self.rewrite_package(package, {f"games/{second['id']}/backups/{backup['id']}/save.zip": b"not a ZIP"})
        storage, manager = self.recipient()
        storage.update_settings({"api_key": "local"})
        before = (storage.data_dir / "library.json").read_bytes()
        with self.assertRaises(ValueError):
            manager.import_library(invalid)
        self.assertEqual(storage.games, [])
        self.assertEqual(storage.settings, {"api_key": "local"})
        self.assertEqual((storage.data_dir / "library.json").read_bytes(), before)
        self.assertFalse((storage.data_dir / "games").exists())

    def test_library_merges_name_keeps_local_paths_images_and_settings(self):
        self.write("slot.sav", "source")
        self.manager.create(self.game)
        source_artwork = self.storage.artwork_dir(self.game)
        source_artwork.mkdir()
        (source_artwork / "cover.png").write_bytes(b"new")
        atomic_write_json(source_artwork / "assets.json", {"assets": {"cover": {"file": "cover.png", "source_url": "source"}}, "missing": ["logo"]})
        self.storage.update_settings({"api_key": "source", "proxy_enabled": True})
        storage, manager = self.recipient()
        local = storage.save_game({"english_name": "test game", "chinese_name": "本机", "save_path": str(self.save)})
        self.write("slot.sav", "local")
        manager.create(local)
        atomic_write_json(storage.game_dir(local) / "sequence.json", {"next_sequence": 200})
        local_artwork = storage.artwork_dir(local)
        local_artwork.mkdir()
        (local_artwork / "cover.png").write_bytes(b"old")
        (local_artwork / "logo.png").write_bytes(b"keep")
        atomic_write_json(local_artwork / "assets.json", {"assets": {"cover": {"file": "cover.png", "source_url": "local"}, "logo": {"file": "logo.png"}}, "missing": []})
        unrelated = storage.save_game({"english_name": "Local Only"})
        storage.update_settings({"api_key": "target", "window_size": [1000, 700]})
        (storage.data_dir / "exports").mkdir()
        (storage.data_dir / "exports/keep.zip").write_bytes(b"keep")
        package = self.manager.export_library()
        self.assertEqual(manager.inspect_library(package)["existing_count"], 1)
        result = manager.import_library(package)
        self.assertEqual((result["new_games"], result["merged_games"], result["imported"]), (0, 1, 1))
        self.assertEqual(storage.get_game(local["id"]), local)
        self.assertEqual(storage.get_game(unrelated["id"]), unrelated)
        self.assertFalse(storage.game_dir(self.game).exists())
        self.assertEqual(manager.list_backups(local)[0]["sequence"], 200)
        self.assertEqual(manager.import_library(package)["skipped"], 1)
        self.assertEqual(manager.create(local)["sequence"], 201)
        self.assertEqual(storage.settings, {"api_key": "source", "window_size": [1000, 700], "proxy_enabled": True})
        manifest = json.loads((local_artwork / "assets.json").read_text())
        self.assertEqual(manifest["assets"]["cover"]["source_url"], "source")
        self.assertEqual(set(manifest["assets"]), {"cover", "logo"})
        self.assertEqual(manifest["missing"], [])
        self.assertEqual((local_artwork / "cover.png").read_bytes(), b"new")
        self.assertEqual((storage.data_dir / "exports/keep.zip").read_bytes(), b"keep")
        self.assertTrue(manager.export_library().is_file())

    def test_library_empty_package_and_resource_only_package(self):
        storage, manager = self.recipient()
        package = manager.export_library()
        self.assertEqual(manager.inspect_library(package), {"game_count": 0, "backup_count": 0, "existing_count": 0,
                                                           "new_count": 0, "orphan_count": 0, "settings_count": 0})
        self.assertEqual(self.manager.import_library(package)["new_games"], 0)
        self.assertEqual(self.storage.games, [self.game])
        self.write("slot.sav", "orphan")
        self.manager.create(self.game)
        self.storage.delete_game(self.game["id"])
        result = manager.import_library(self.manager.export_library())
        self.assertEqual((result["orphan_count"], result["imported"]), (1, 1))
        self.assertEqual(storage.games, [])
        self.assertEqual(len(manager.list_backups(self.game)), 1)

    def test_library_rejects_identity_and_resource_routing_conflicts(self):
        second = self.storage.save_game({"english_name": "Second Game"})
        storage, manager = self.recipient()
        storage.save_game({**second, "english_name": self.game["english_name"]}, allow_new_id=True)
        package = self.manager.export_library()
        with self.assertRaisesRegex(ValueError, "多个游戏资源"), mock.patch.object(storage, "replace_library") as commit:
            manager.import_library(package)
        commit.assert_not_called()
        self.assertEqual(len(storage.games), 1)
        self.assertFalse((storage.data_dir / "games").exists())
        self.storage.game_dir(second).mkdir(parents=True)
        self.storage.delete_game(second["id"])
        with self.assertRaisesRegex(ValueError, "孤留资源"):
            manager.inspect_library(self.manager.export_library())
        storage.save_game({**self.game, "english_name": "Different Local Game"}, allow_new_id=True)
        with self.assertRaisesRegex(ValueError, "分别对应不同游戏"):
            manager.import_library(self.manager.export_library())

    def test_library_invalid_headers_and_paths_have_no_side_effects(self):
        self.write("slot.sav", "saved")
        backup = self.manager.create(self.game)
        package = self.manager.export_library()
        storage, manager = self.recipient()
        manager.import_library(package)
        before = {path.relative_to(storage.data_dir): path.read_bytes() for path in storage.data_dir.rglob("*") if path.is_file()}
        library = {"games": [self.game], "settings": {}}
        prefix = f"games/{self.game['id']}/backups/{backup['id']}/"
        cases = [({"../outside": b"bad"}, ()), ({"exports/recursive.zip": b"bad"}, ()),
                 ({f"games/{self.game['id']}/artwork/Cover.png": b"one", f"games/{self.game['id']}/artwork/cover.png": b"two"}, ()),
                 ({"library.json": b'{}'}, ()), ({"library.json": json.dumps({**library, "games": [self.game, self.game]}).encode()}, ()),
                 ({f"games/{self.game['id']}/sequence.json": b'{"next_sequence":false}'}, ()),
                 ({}, (prefix + "metadata.json",))]
        for updates, omitted in cases:
            invalid = self.rewrite_package(package, updates, omitted)
            with self.subTest(updates=list(updates), omitted=omitted), self.assertRaises(ValueError):
                manager.import_library(invalid)
            self.assertEqual({path.relative_to(storage.data_dir): path.read_bytes() for path in storage.data_dir.rglob("*") if path.is_file()}, before)
        self.assertEqual(list(storage.data_dir.glob(".library-*")), [])

    def test_library_outer_and_inner_crc_and_inner_paths_are_rejected(self):
        self.write("slot.sav", "saved")
        backup = self.manager.create(self.game)
        artwork = self.storage.artwork_dir(self.game)
        artwork.mkdir()
        (artwork / "cover.png").write_bytes(b"unique_image")
        package = self.manager.export_library()
        storage, manager = self.recipient()
        outer = self.root / "outer.zip"
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_STORED) as target:
            for name in source.namelist():
                target.writestr(name, source.read(name))
        outer.write_bytes(outer.read_bytes().replace(b"unique_image", b"broken_image"))
        with self.assertRaises(ValueError):
            manager.import_library(outer)
        prefix = f"games/{self.game['id']}/backups/{backup['id']}/"
        metadata = json.loads((self.manager.backup_dir(self.game, backup["id"]) / "metadata.json").read_text())
        inner = self.root / "inner.zip"
        for name in ("../outside", "crc.sav", "NUL.txt"):
            with zipfile.ZipFile(inner, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(name, b"unique-content")
            data = inner.read_bytes()
            if name == "crc.sav":
                data = data.replace(b"unique-content", b"broken-content")
            invalid = self.rewrite_package(package, {prefix + "save.zip": data,
                                                     prefix + "metadata.json": json.dumps({**metadata, "size": len(data)}).encode()})
            with self.subTest(name=name), self.assertRaises(ValueError):
                manager.import_library(invalid)
            self.assertEqual(storage.games, [])
            self.assertFalse((storage.data_dir / "games").exists())

    def test_library_configuration_failure_rolls_back_resources_memory_and_disk(self):
        self.write("slot.sav", "first")
        self.manager.create(self.game)
        storage, manager = self.recipient()
        manager.import_library(self.manager.export_library())
        before = {path.relative_to(storage.data_dir): path.read_bytes() for path in storage.data_dir.rglob("*") if path.is_file()}
        memory = json.loads((storage.data_dir / "library.json").read_text())
        self.write("slot.sav", "second")
        self.manager.create(self.game)
        package = self.manager.export_library()
        original_replace = os.replace

        def fail_configuration(source, destination):
            if Path(destination) == storage.data_dir / "library.json":
                storage.settings["api_key"] = "partial mutation"
                raise PermissionError("simulated configuration failure")
            return original_replace(source, destination)

        with mock.patch("game_manager.storage.os.replace", side_effect=fail_configuration), self.assertRaises(PermissionError):
            manager.import_library(package)
        self.assertEqual((storage.games, storage.settings), (memory["games"], memory["settings"]))
        self.assertEqual({path.relative_to(storage.data_dir): path.read_bytes() for path in storage.data_dir.rglob("*") if path.is_file()}, before)
        self.assertEqual(list(storage.data_dir.glob(".library-*")), [])

    def test_library_directory_publish_failure_and_failed_rollback_report_old_root(self):
        self.write("slot.sav", "saved")
        self.manager.create(self.game)
        package = self.manager.export_library()
        storage, manager = self.recipient()
        manager.import_library(package)
        root = storage.data_dir / "games"
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        original_rename = Path.rename

        def fail_publish(path, target):
            if path.name == "games" and path.parent.name == "merged":
                raise PermissionError("simulated games root lock")
            return original_rename(path, target)

        with mock.patch.object(Path, "rename", fail_publish), self.assertRaises(PermissionError):
            manager.import_library(package)
        self.assertEqual({path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)

        def fail_rollback(path, target):
            if path.name.startswith(".library-previous-"):
                raise PermissionError("simulated rollback lock")
            return fail_publish(path, target)

        with mock.patch.object(Path, "rename", fail_rollback), self.assertRaisesRegex(RuntimeError, "保留的原游戏资源位于") as caught:
            manager.import_library(package)
        previous = next(storage.data_dir.glob(".library-previous-*"))
        self.assertIn(str(previous), str(caught.exception))
        self.assertEqual({path.relative_to(previous): path.read_bytes() for path in previous.rglob("*") if path.is_file()}, before)

    def test_library_export_failure_and_local_symlink_leave_resources(self):
        self.write("slot.sav", "saved")
        backup = self.manager.create(self.game)
        archive = self.manager.backup_dir(self.game, backup["id"]) / "save.zip"
        archive.write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            self.manager.export_library()
        self.assertEqual(list((self.storage.data_dir / "exports").iterdir()), [])
        archive.unlink()
        self.manager.delete(self.game, backup["id"])
        outside = self.root / "outside"
        outside.write_text("keep")
        link = self.storage.artwork_dir(self.game)
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("当前系统不允许创建符号链接")
        with self.assertRaises(ValueError):
            self.manager.export_library()
        empty_storage, empty_manager = self.recipient()
        with self.assertRaises(ValueError):
            self.manager.import_library(empty_manager.export_library())
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue(link.is_symlink())

    def test_library_storage_validation_deep_copy_and_atomic_failure(self):
        value = {"games": [{**self.game, "custom": {"tags": ["keep"]}}], "settings": {"nested": {"enabled": True}}}
        games, settings = Storage.validate_library(value)
        games[0]["custom"]["tags"].append("changed")
        settings["nested"]["enabled"] = False
        self.assertEqual(value["games"][0]["custom"]["tags"], ["keep"])
        self.assertTrue(value["settings"]["nested"]["enabled"])
        self.storage.replace_library(value)
        value["settings"]["nested"]["enabled"] = False
        self.assertTrue(self.storage.settings["nested"]["enabled"])
        before = (self.storage.data_dir / "library.json").read_bytes()
        with mock.patch("game_manager.storage.os.replace", side_effect=PermissionError("simulated storage write failure")), self.assertRaises(PermissionError):
            self.storage.replace_library({"games": [], "settings": {}})
        self.assertEqual((self.storage.data_dir / "library.json").read_bytes(), before)
        self.assertEqual(len(self.storage.games), 1)
        self.assertTrue(self.storage.settings["nested"]["enabled"])

    def test_library_new_configuration_failure_copy_failure_and_walk_failure_publish_nothing(self):
        self.write("slot.sav", "saved")
        self.manager.create(self.game)
        package = self.manager.export_library()
        storage, manager = self.recipient()
        with mock.patch.object(storage, "replace_library", side_effect=PermissionError("simulated configuration failure")), self.assertRaises(PermissionError):
            manager.import_library(package)
        self.assertEqual(storage.games, [])
        self.assertFalse((storage.data_dir / "library.json").exists())
        self.assertFalse((storage.data_dir / "games").exists())
        manager.import_library(package)
        before = {path.relative_to(storage.data_dir): path.read_bytes() for path in storage.data_dir.rglob("*") if path.is_file()}
        with mock.patch("game_manager.backups.shutil.copytree", side_effect=PermissionError("simulated resource copy failure")), self.assertRaises(PermissionError):
            manager.import_library(package)
        self.assertEqual({path.relative_to(storage.data_dir): path.read_bytes() for path in storage.data_dir.rglob("*") if path.is_file()}, before)
        exports = set((self.storage.data_dir / "exports").iterdir())

        def fail_walk(path, *args, **kwargs):
            kwargs["onerror"](PermissionError("simulated directory read failure"))
            return iter(())

        with mock.patch("game_manager.backups.os.walk", side_effect=fail_walk), self.assertRaises(PermissionError):
            self.manager.export_library()
        self.assertEqual(set((self.storage.data_dir / "exports").iterdir()), exports)
        self.assertTrue(self.storage.game_dir(self.game).is_dir())
        self.assertEqual(list(storage.data_dir.glob(".library-*")), [])

    def test_import_artwork_file_directory_collision_preserves_local_tree(self):
        storage, manager = self.recipient()
        manager.import_game(self.manager.export(self.game))
        local = storage.artwork_dir(self.game)
        (local / "hero.png").mkdir(parents=True)
        (local / "hero.png/keep.txt").write_text("keep")
        source = self.storage.artwork_dir(self.game)
        source.mkdir(parents=True)
        (source / "hero.png").write_bytes(b"hero image")
        atomic_write_json(source / "assets.json", {"assets": {"hero": {"file": "hero.png"}}, "missing": []})
        with self.assertRaisesRegex(ValueError, "目录结构冲突"):
            manager.import_game(self.manager.export(self.game))
        self.assertEqual((local / "hero.png/keep.txt").read_text(), "keep")
        self.assertFalse((local / "hero.png/hero.png").exists())


if __name__ == "__main__":
    unittest.main()
