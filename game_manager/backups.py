"""目录存档的备份、完整还原和离线导出。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from .storage import Storage, atomic_write_json


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _raise_walk_error(error: OSError) -> None:
    raise error


def _contains(parent: Path, child: Path) -> bool:
    if parent == child or parent in child.parents:
        return True
    if parent.exists():
        for candidate in (child, *child.parents):
            if candidate.exists() and os.path.samefile(parent, candidate):
                return True
    return False


def _validate_members(archive: zipfile.ZipFile) -> None:
    entries, spelling = {}, {}
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    for member in archive.infolist():
        name = member.filename
        parts = name.rstrip("/").split("/")
        if (not name or "\x00" in member.orig_filename or name.startswith("/") or "\\" in name
                or (archive.mode == "r" and "\\" in member.orig_filename)):
            raise ValueError(f"备份包含不安全的路径：{name!r}")
        for part in parts:
            if (part in ("", ".", "..") or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                    or part.endswith((" ", ".")) or part.split(".")[0].upper() in reserved):
                raise ValueError(f"备份包含不安全或不兼容 Windows 的名称：{name!r}")
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or member.flag_bits & 1:
            raise ValueError(f"备份包含链接、特殊文件或加密文件：{name!r}")
        key = "/".join(parts).casefold()
        if key in entries:
            raise ValueError(f"备份包含重复路径：{name!r}")
        entries[key] = member.is_dir()
        for i in range(1, len(parts) + 1):
            original = "/".join(parts[:i])
            folded = original.casefold()
            if folded in spelling and spelling[folded] != original:
                raise ValueError(f"备份包含大小写冲突：{name!r}")
            spelling[folded] = original
    for key in entries:
        parts = key.split("/")
        for i in range(1, len(parts)):
            if entries.get("/".join(parts[:i])) is False:
                raise ValueError("备份的文件与目录路径冲突")


class BackupManager:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._lock = threading.RLock()
        self.application_dir = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                                else Path(__file__).resolve().parent.parent)

    def _save_path(self, game: dict, must_exist: bool = False) -> Path:
        raw = game.get("save_path", "")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("请先填写存档目录")
        expanded = os.path.expandvars(os.path.expanduser(raw))
        expanded = re.sub(r"%([^%]+)%", lambda match: os.environ.get(match[1], match[0]), expanded)
        original = Path(expanded)
        if _is_link(original):
            raise ValueError("存档目录不能是符号链接或目录联接")
        path = original.resolve()
        home = Path.home().resolve()
        if path == Path(path.anchor) or (_contains(home, path) and _contains(path, home)):
            raise ValueError("不能将磁盘根目录或用户主目录作为存档目录")
        if _contains(path, self.application_dir):
            raise ValueError("存档目录不能是应用目录或包含应用目录")
        if _contains(path, self.storage.data_dir) or _contains(self.storage.data_dir, path):
            raise ValueError("存档目录不能与数据目录重叠")
        if path.exists() and not path.is_dir():
            raise ValueError("存档路径必须是目录")
        if must_exist and not path.is_dir():
            raise ValueError(f"存档目录不存在：{path}")
        return path

    def _root(self, game: dict) -> Path:
        directory = self.storage.game_dir(game)
        root = directory / "backups"
        if any(_is_link(path) for path in (directory.parent, directory, root)):
            raise ValueError("游戏数据目录不能是链接")
        return root

    def backup_dir(self, game: dict, backup_id: str) -> Path:
        if not isinstance(backup_id, str) or not re.fullmatch(r"\d+_\d{8}_\d{6}", backup_id):
            raise ValueError("备份 ID 无效")
        path = self._root(game) / backup_id
        if _is_link(path) or _is_link(path.parent) or _is_link(self.storage.game_dir(game)):
            raise ValueError("备份目录不能是链接")
        return path

    def list_backups(self, game: dict) -> list[dict]:
        with self._lock:
            root = self._root(game)
            if not root.exists():
                return []
            records = []
            for directory in root.iterdir():
                if directory.name.startswith(".") or not directory.is_dir():
                    continue
                self.backup_dir(game, directory.name)
                try:
                    if _is_link(directory / "metadata.json") or _is_link(directory / "save.zip"):
                        raise ValueError("备份文件不能是链接")
                    record = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
                    if (not isinstance(record, dict) or record["id"] != directory.name
                            or type(record["sequence"]) is not int or record["sequence"] < 1
                            or record["sequence"] != int(directory.name.split("_")[0])
                            or not isinstance(record["reason"], str) or type(record["size"]) is not int
                            or record["size"] < 0
                            or not (directory / "save.zip").is_file()):
                        raise ValueError("备份记录无效")
                    created = datetime.fromisoformat(record["created_at"])
                    if created.utcoffset() is None or created.microsecond:
                        raise ValueError("备份时间无效")
                except (OSError, KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"备份记录损坏：{directory}") from error
                records.append(record)
            return sorted(records, key=lambda record: record["sequence"], reverse=True)

    def _new_record(self, game: dict, reason: str, write_archive) -> dict:
        root = self._root(game)
        root.mkdir(parents=True, exist_ok=True)
        counter = self.storage.game_dir(game) / "sequence.json"
        try:
            if _is_link(counter):
                raise ValueError("序号文件不能是链接")
            sequence = json.loads(counter.read_text(encoding="utf-8"))["next_sequence"] if counter.exists() else 1
            if type(sequence) is not int or sequence < 1:
                raise ValueError("序号无效")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"备份序号文件损坏：{counter}") from error
        existing = self.list_backups(game)
        sequence = max(sequence, existing[0]["sequence"] + 1 if existing else 1)
        atomic_write_json(counter, {"next_sequence": sequence + 1})
        now = datetime.now().astimezone().replace(microsecond=0)
        backup_id = f"{sequence:06d}_{now:%Y%m%d_%H%M%S}"
        destination = self.backup_dir(game, backup_id)
        temporary = root / f".tmp-{uuid.uuid4().hex}"
        temporary.mkdir()
        try:
            archive_path = temporary / "save.zip"
            write_archive(archive_path)
            record = {"id": backup_id, "sequence": sequence, "created_at": now.isoformat(timespec="seconds"),
                      "reason": reason, "size": archive_path.stat().st_size}
            atomic_write_json(temporary / "metadata.json", record)
            temporary.rename(destination)
            return record
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _write_source(source: Path, archive_path: Path) -> None:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for directory, directories, files in os.walk(source, followlinks=False, onerror=_raise_walk_error):
                directory = Path(directory)
                for name in sorted(directories + files):
                    path = directory / name
                    if _is_link(path) or not (path.is_dir() or stat.S_ISREG(path.stat().st_mode)):
                        raise ValueError(f"存档中包含链接或特殊文件：{path}")
                    archive.write(path, path.relative_to(source).as_posix())
            _validate_members(archive)

    def create(self, game: dict, reason: str = "manual") -> dict:
        with self._lock:
            source = self._save_path(game, must_exist=True)
            return self._new_record(game, reason, lambda path: self._write_source(source, path))

    def copy(self, game: dict, backup_id: str) -> dict:
        with self._lock:
            source = self.backup_dir(game, backup_id) / "save.zip"
            if not source.is_file() or _is_link(source):
                raise ValueError("备份文件不存在或无效")
            with zipfile.ZipFile(source) as archive:
                _validate_members(archive)
                if archive.testzip() is not None:
                    raise ValueError("备份文件校验失败")
            return self._new_record(game, "copy", lambda path: shutil.copyfile(source, path))

    def delete(self, game: dict, backup_id: str) -> None:
        with self._lock:
            directory = self.backup_dir(game, backup_id)
            if not directory.is_dir():
                raise ValueError("备份记录不存在")
            shutil.rmtree(directory)

    def delete_many(self, game: dict, backup_ids: list[str]) -> int:
        with self._lock:
            directories = [self.backup_dir(game, backup_id) for backup_id in dict.fromkeys(backup_ids)]
            for directory in directories:
                if not directory.is_dir():
                    raise ValueError(f"备份记录不存在：{directory.name}")
            deleted = 0
            for directory in directories:
                try:
                    shutil.rmtree(directory)
                except OSError as error:
                    raise RuntimeError(f"删除备份失败：已删除 {deleted} / 共 {len(directories)} 条；"
                                       f"当前失败记录：{directory.name}。{error}") from error
                deleted += 1
            return deleted

    def restore(self, game: dict, backup_id: str) -> str | None:
        with self._lock:
            destination = self._save_path(game)
            archive_path = self.backup_dir(game, backup_id) / "save.zip"
            if not archive_path.is_file() or _is_link(archive_path):
                raise ValueError("备份文件不存在或无效")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.parent / f".gsm-restore-{uuid.uuid4().hex}"
            previous = destination.parent / f".gsm-previous-{uuid.uuid4().hex}"
            temporary.mkdir()
            moved = False
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    _validate_members(archive)
                    for member in archive.infolist():
                        output = temporary.joinpath(*member.filename.rstrip("/").split("/"))
                        if member.is_dir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, output.open("xb") as target:
                                shutil.copyfileobj(source, target)
                if destination.exists():
                    destination.rename(previous)
                    moved = True
                try:
                    temporary.rename(destination)
                except OSError as error:
                    if moved:
                        try:
                            previous.rename(destination)
                        except OSError as rollback_error:
                            raise RuntimeError(f"还原失败，原存档保留在 {previous}，请手动恢复。") from rollback_error
                    raise error
                warning = None
                if moved:
                    try:
                        shutil.rmtree(previous)
                    except OSError:
                        warning = f"还原成功，但旧目录未能清理：{previous}"
                return warning
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)

    def export(self, game: dict) -> Path:
        with self._lock:
            records = self.list_backups(game)
            for record in records:
                path = self.backup_dir(game, record["id"]) / "save.zip"
                try:
                    with zipfile.ZipFile(path) as archive:
                        _validate_members(archive)
                        damaged = archive.testzip()
                        if damaged is not None:
                            raise ValueError(f"文件校验失败：{damaged}")
                except Exception as error:
                    raise ValueError(f"备份 #{record['sequence']:06d} 校验失败，无法导出：{error}") from error
            exports = self.storage.data_dir / "exports"
            exports.mkdir(parents=True, exist_ok=True)
            now = datetime.now().astimezone()
            name = re.sub(r"[^A-Za-z0-9_-]", "_", game["english_name"])[:60] or "game"
            destination = exports / f"{name}_{game['id']}_{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.zip"
            temporary = exports / f".tmp-{uuid.uuid4().hex}"
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    metadata = {key: game.get(key, "") for key in
                                ("id", "english_name", "chinese_name", "game_path", "save_path", "steamgrid_id")}
                    archive.writestr("game.json", json.dumps(metadata, ensure_ascii=False, indent=2))
                    for record in records:
                        directory = self.backup_dir(game, record["id"])
                        for name in ("metadata.json", "save.zip"):
                            path = directory / name
                            if _is_link(path):
                                raise ValueError("备份文件不能是链接")
                            archive.write(path, f"backups/{record['id']}/{name}")
                    artwork = self.storage.artwork_dir(game)
                    if artwork.exists():
                        if _is_link(artwork):
                            raise ValueError("图片目录不能是链接")
                        for directory, directories, files in os.walk(artwork, followlinks=False, onerror=_raise_walk_error):
                            directories[:] = [name for name in directories if not name.startswith(".")]
                            for name in sorted(directories + files):
                                path = Path(directory) / name
                                if _is_link(path) or not (path.is_dir() or stat.S_ISREG(path.stat().st_mode)):
                                    raise ValueError("图片目录中包含链接或特殊文件")
                                if not name.startswith("."):
                                    archive.write(path, f"artwork/{path.relative_to(artwork).as_posix()}")
                    _validate_members(archive)
                temporary.rename(destination)
                return destination
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _import_header(archive: zipfile.ZipFile) -> tuple[dict, list[str], bool]:
        _validate_members(archive)
        groups = {}
        has_artwork = False
        for member in archive.infolist():
            parts = member.filename.rstrip("/").split("/")
            if parts == ["game.json"] and not member.is_dir():
                continue
            if parts[0] == "backups":
                if len(parts) == 1 and member.is_dir():
                    continue
                if len(parts) < 2 or not re.fullmatch(r"[0-9]+_[0-9]{8}_[0-9]{6}", parts[1]):
                    raise ValueError("导入包包含无效备份目录")
                files = groups.setdefault(parts[1], set())
                if len(parts) == 2 and member.is_dir():
                    continue
                if len(parts) != 3 or member.is_dir() or parts[2] not in ("metadata.json", "save.zip"):
                    raise ValueError("导入包的备份结构无效")
                files.add(parts[2])
            elif parts[0] == "artwork" and (len(parts) > 1 or member.is_dir()):
                has_artwork = has_artwork or not member.is_dir()
            else:
                raise ValueError(f"导入包包含不支持的项目：{member.filename}")
        if any(files != {"metadata.json", "save.zip"} for files in groups.values()):
            raise ValueError("导入包中的备份缺少 metadata.json 或 save.zip")
        try:
            value = json.loads(archive.read("game.json"))
            keys = ("id", "english_name", "chinese_name", "game_path", "save_path", "steamgrid_id")
            game = {key: value[key] for key in keys}
            Storage._validate_id(game["id"])
            if any(not isinstance(game[key], str) for key in keys[1:5]) or not game["english_name"].strip():
                raise ValueError("游戏名称或目录配置无效")
            if type(game["steamgrid_id"]) not in (str, int):
                raise ValueError("SteamGridDB 游戏 ID 无效")
            game["english_name"] = game["english_name"].strip()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("导入包的 game.json 缺失或游戏资料无效") from error
        return game, sorted(groups), has_artwork

    def _import_match(self, game: dict) -> dict | None:
        by_id = next((item for item in self.storage.games if item["id"] == game["id"]), None)
        by_name = next((item for item in self.storage.games
                        if item["english_name"].strip().casefold() == game["english_name"].casefold()), None)
        if by_id and by_name and by_id["id"] != by_name["id"]:
            raise ValueError("导入游戏的 ID 和英文名分别对应不同游戏，无法合并")
        match = by_id or by_name
        return dict(match) if match else None

    def inspect_import(self, path: Path) -> dict:
        with self._lock, self.storage._lock:
            try:
                with zipfile.ZipFile(path) as archive:
                    game, backup_ids, has_artwork = self._import_header(archive)
                return {"game": game, "existing_game": self._import_match(game),
                        "backup_count": len(backup_ids), "has_artwork": has_artwork}
            except zipfile.BadZipFile as error:
                raise ValueError("导入文件不是完整有效的 ZIP 游戏包") from error

    @staticmethod
    def _local_tree(directory: Path) -> None:
        if _is_link(directory) or not directory.is_dir():
            raise ValueError(f"本机游戏资源不是普通目录：{directory}")
        for parent, directories, files in os.walk(directory, followlinks=False, onerror=_raise_walk_error):
            for name in directories + files:
                path = Path(parent) / name
                if _is_link(path) or not (path.is_dir() or stat.S_ISREG(path.stat().st_mode)):
                    raise ValueError(f"本机游戏资源包含链接或特殊文件：{path}")

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _validated_backup_records(self, source_game: dict, incoming: Path, backup_ids: list[str]) -> list:
        records, sequences = [], set()
        for backup_id in backup_ids:
            directory = incoming / "backups" / backup_id
            archive_path = directory / "save.zip"
            try:
                record = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
                if (record["id"] != backup_id or type(record["sequence"]) is not int or record["sequence"] < 1
                        or record["sequence"] != int(backup_id.split("_")[0])
                        or not isinstance(record["reason"], str) or type(record["size"]) is not int
                        or record["size"] != archive_path.stat().st_size
                        or not isinstance(record["created_at"], str)
                        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}",
                                            record["created_at"])):
                    raise ValueError("备份元数据无效")
                created = datetime.fromisoformat(record["created_at"])
                if created.utcoffset() is None or created.microsecond or backup_id.split("_", 1)[1] != f"{created:%Y%m%d_%H%M%S}":
                    raise ValueError("备份时间与目录不一致")
                if record["sequence"] in sequences:
                    raise ValueError("备份序号重复")
                sequences.add(record["sequence"])
                with zipfile.ZipFile(archive_path) as archive:
                    _validate_members(archive)
                    if archive.testzip() is not None:
                        raise ValueError("备份 CRC 校验失败")
                digest = self._file_hash(archive_path)
                key = record.get("import_key", f"{source_game['id']}:{backup_id}:{digest}")
                if (not isinstance(key, str)
                        or not re.fullmatch(r"[0-9a-f]{32}:[0-9]+_[0-9]{8}_[0-9]{6}:[0-9a-f]{64}", key)
                        or key.rsplit(":", 1)[1] != digest):
                    raise ValueError("备份来源标识无效")
                records.append((record, archive_path, digest, key))
            except Exception as error:
                raise ValueError(f"导入备份 {backup_id} 校验失败：{error}") from error
        return records

    def _import_backups(self, source_game: dict, game: dict, incoming: Path, merged: Path,
                        backup_ids: list[str]) -> tuple[int, int]:
        records = self._validated_backup_records(source_game, incoming, backup_ids)
        sequences = {record["sequence"] for record, _, _, _ in records}
        local = self.list_backups(game)
        known = set()
        for record in local:
            digest = self._file_hash(self.backup_dir(game, record["id"]) / "save.zip")
            known.add(f"{game['id']}:{record['id']}:{digest}")
            key = record.get("import_key")
            if isinstance(key, str) and key.endswith(":" + digest):
                known.add(key)
        counter = merged / "sequence.json"
        try:
            next_sequence = json.loads(counter.read_text(encoding="utf-8"))["next_sequence"] if counter.exists() else 1
            if type(next_sequence) is not int or next_sequence < 1:
                raise ValueError("序号无效")
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("本机备份序号文件损坏") from error
        used_ids = {record["id"] for record in local}
        used_sequences = {record["sequence"] for record in local}
        next_sequence = max(next_sequence, max(used_sequences | sequences, default=0) + 1)
        root = merged / "backups"
        root.mkdir(exist_ok=True)
        imported = skipped = 0
        for record, source, digest, key in records:
            if key in known:
                skipped += 1
                continue
            record = dict(record)
            if record["id"] in used_ids or record["sequence"] in used_sequences:
                record["sequence"] = next_sequence
                created = datetime.fromisoformat(record["created_at"])
                record["id"] = f"{next_sequence:06d}_{created:%Y%m%d_%H%M%S}"
                next_sequence += 1
            record["import_key"] = key
            directory = root / record["id"]
            directory.mkdir()
            shutil.copyfile(source, directory / "save.zip")
            atomic_write_json(directory / "metadata.json", record)
            used_ids.add(record["id"])
            used_sequences.add(record["sequence"])
            known.add(key)
            imported += 1
        atomic_write_json(counter, {"next_sequence": next_sequence})
        return imported, skipped

    @staticmethod
    def _artwork_manifest(directory: Path) -> dict | None:
        path = directory / "assets.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("图片来源必须是对象")
            assets, missing = value.get("assets", {}), value.get("missing", [])
            if not isinstance(assets, dict) or not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
                raise ValueError("图片来源结构无效")
            for kind, asset in assets.items():
                filename = asset["file"]
                if (not isinstance(kind, str) or not isinstance(filename, str) or filename.startswith("/")
                        or any(part in ("", ".", "..") for part in filename.split("/"))
                        or re.search(r'[<>:"|?*\\\x00-\x1f]', filename)
                        or not directory.joinpath(*filename.split("/")).is_file()):
                    raise ValueError("图片来源指向无效文件")
            return value
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"图片来源文件无效：{path}") from error

    def _import_artwork(self, incoming: Path, merged: Path) -> None:
        source = incoming / "artwork"
        if not source.exists():
            return
        destination = merged / "artwork"
        local_manifest = self._artwork_manifest(destination)
        source_manifest = self._artwork_manifest(source)
        paths = list(source.rglob("*"))
        local_names = {path.relative_to(destination).as_posix().casefold(): path.relative_to(destination).as_posix()
                       for path in destination.rglob("*")}
        for path in paths:
            relative = path.relative_to(source)
            name = relative.as_posix()
            if name.casefold() in local_names and local_names[name.casefold()] != name:
                raise ValueError(f"导入图片与本机名称大小写冲突：{name}")
            target = destination / relative
            if target.exists() and path.is_dir() != target.is_dir():
                raise ValueError(f"导入图片与本机目录结构冲突：{relative}")
        replaced = {path.relative_to(source).as_posix().casefold() for path in paths if path.is_file()}
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        if local_manifest is None and source_manifest is None:
            return
        local_assets = (local_manifest or {}).get("assets", {})
        source_assets = (source_manifest or {}).get("assets", {})
        assets = {kind: asset for kind, asset in local_assets.items()
                  if asset["file"].casefold() not in replaced or kind in source_assets}
        assets.update(source_assets)
        value = {**(local_manifest or {}), **(source_manifest or {}), "assets": assets}
        missing = dict.fromkeys((local_manifest or {}).get("missing", []) + (source_manifest or {}).get("missing", []))
        value["missing"] = [kind for kind in missing if kind not in assets]
        atomic_write_json(destination / "assets.json", value)
        self._artwork_manifest(destination)

    def import_game(self, path: Path) -> dict:
        with self._lock, self.storage._lock:
            games_root = self.storage.data_dir / "games"
            if _is_link(games_root):
                raise ValueError("本机游戏数据目录不能是链接")
            root_existed = games_root.exists()
            temporary = previous = destination = None
            moved = published = False
            memory = copy.deepcopy((self.storage.games, self.storage.settings))
            try:
                with zipfile.ZipFile(path) as archive:
                    source_game, backup_ids, _ = self._import_header(archive)
                    game = self._import_match(source_game) or source_game
                    destination = self.storage.game_dir(game)
                    self._root(game)
                    if destination.exists():
                        self._local_tree(destination)
                    games_root.mkdir(parents=True, exist_ok=True)
                    temporary = games_root / f".import-{uuid.uuid4().hex}"
                    temporary.mkdir()
                    incoming = temporary / "incoming"
                    incoming.mkdir()
                    for member in archive.infolist():
                        output = incoming.joinpath(*member.filename.rstrip("/").split("/"))
                        if member.is_dir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, output.open("xb") as target:
                                shutil.copyfileobj(source, target)
                merged = temporary / "merged"
                if destination.exists():
                    shutil.copytree(destination, merged, symlinks=True)
                    self._local_tree(merged)
                else:
                    merged.mkdir()
                imported, skipped = self._import_backups(source_game, game, incoming, merged, backup_ids)
                self._import_artwork(incoming, merged)
                self._local_tree(merged)
                previous = games_root / f".previous-{uuid.uuid4().hex}"
                if destination.exists():
                    destination.rename(previous)
                    moved = True
                merged.rename(destination)
                published = True
                final_game = self.storage.save_game(game, allow_new_id=True)
            except Exception as error:
                self.storage.games, self.storage.settings = memory
                try:
                    if published:
                        destination.rename(temporary / "failed")
                    if moved:
                        previous.rename(destination)
                except OSError as rollback_error:
                    location = previous if moved else destination
                    raise RuntimeError(f"导入失败且回滚未完成，保留的游戏资源位于 {location}，请手动恢复。") from rollback_error
                if isinstance(error, zipfile.BadZipFile):
                    raise ValueError("导入包或内部备份 ZIP 已损坏，未导入任何内容") from error
                raise
            else:
                result = {"game": final_game, "imported": imported, "skipped": skipped}
                if moved:
                    try:
                        shutil.rmtree(previous)
                    except OSError:
                        result["cleanup_warning"] = f"导入成功，但旧资源目录未能清理：{previous}"
                return result
            finally:
                if temporary is not None and temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
                if not root_existed and games_root.exists() and not any(games_root.iterdir()):
                    games_root.rmdir()

    @staticmethod
    def _library_header(archive: zipfile.ZipFile) -> tuple[dict, dict[str, list[str]]]:
        _validate_members(archive)
        resources = {}
        for member in archive.infolist():
            parts = member.filename.rstrip("/").split("/")
            if parts == ["library.json"] and not member.is_dir():
                continue
            if parts == ["games"] and member.is_dir():
                continue
            if parts[0] != "games" or len(parts) < 2:
                raise ValueError(f"资料库包包含不支持的项目：{member.filename}")
            Storage._validate_id(parts[1])
            groups = resources.setdefault(parts[1], {})
            if len(parts) == 2 and member.is_dir():
                continue
            if len(parts) >= 3 and parts[2] == "artwork" and (len(parts) > 3 or member.is_dir()):
                continue
            if len(parts) == 3 and parts[2] == "sequence.json" and not member.is_dir():
                continue
            if len(parts) >= 3 and parts[2] == "backups":
                if len(parts) == 3 and member.is_dir():
                    continue
                if len(parts) < 4 or not re.fullmatch(r"[0-9]+_[0-9]{8}_[0-9]{6}", parts[3]):
                    raise ValueError("资料库包包含无效备份目录")
                files = groups.setdefault(parts[3], set())
                if len(parts) == 4 and member.is_dir():
                    continue
                if len(parts) == 5 and not member.is_dir() and parts[4] in ("metadata.json", "save.zip"):
                    files.add(parts[4])
                    continue
            raise ValueError(f"资料库包的游戏资源结构无效：{member.filename}")
        if any(files != {"metadata.json", "save.zip"} for groups in resources.values() for files in groups.values()):
            raise ValueError("资料库包中的备份缺少 metadata.json 或 save.zip")
        try:
            games, settings = Storage.validate_library(json.loads(archive.read("library.json")))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("资料库包的 library.json 缺失或资料无效") from error
        return {"games": games, "settings": settings}, {key: sorted(groups) for key, groups in resources.items()}

    def _library_routes(self, library: dict, resources: dict) -> tuple[dict, list[dict], int]:
        games = copy.deepcopy(self.storage.games)
        routes, destinations = {}, set()
        existing = 0
        for source in library["games"]:
            match = self._import_match({**source, "english_name": source["english_name"].strip()})
            target = match or source
            if target["id"] in destinations:
                raise ValueError("资料库中的多个游戏资源对应同一本机游戏，无法合并")
            routes[source["id"]] = target["id"]
            destinations.add(target["id"])
            if match:
                existing += 1
            else:
                games.append(copy.deepcopy(source))
        for game_id in resources:
            if game_id not in routes:
                if game_id in destinations:
                    raise ValueError("资料库中的孤留资源与游戏合并目标冲突")
                routes[game_id] = game_id
                destinations.add(game_id)
        return routes, games, existing

    @staticmethod
    def _library_counter(directory: Path) -> int:
        path = directory / "sequence.json"
        if not path.exists():
            return 1
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["next_sequence"]
            if _is_link(path) or type(value) is not int or value < 1:
                raise ValueError("序号无效")
            return value
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"资料库备份序号文件损坏：{path}") from error

    def inspect_library(self, path: Path) -> dict:
        with self._lock, self.storage._lock:
            try:
                with zipfile.ZipFile(path) as archive:
                    library, resources = self._library_header(archive)
                _, _, existing = self._library_routes(library, resources)
                ids = {game["id"] for game in library["games"]}
                return {"game_count": len(ids), "backup_count": sum(map(len, resources.values())),
                        "existing_count": existing, "new_count": len(ids) - existing,
                        "orphan_count": len(set(resources) - ids), "settings_count": len(library["settings"])}
            except zipfile.BadZipFile as error:
                raise ValueError("导入文件不是完整有效的 ZIP 资料库包") from error

    def export_library(self) -> Path:
        with self._lock, self.storage._lock:
            games, settings = Storage.validate_library({"games": self.storage.games, "settings": self.storage.settings})
            root = self.storage.data_dir / "games"
            if root.exists() or _is_link(root):
                self._local_tree(root)
            exports = self.storage.data_dir / "exports"
            if _is_link(exports):
                raise ValueError("导出目录不能是链接")
            exports.mkdir(parents=True, exist_ok=True)
            now = datetime.now().astimezone()
            destination = exports / f"Library_{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.zip"
            temporary = exports / f".tmp-{uuid.uuid4().hex}"
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("library.json", json.dumps({"games": games, "settings": settings}, ensure_ascii=False, indent=2))
                    if root.exists():
                        for directory in sorted(root.iterdir()):
                            if directory.name.startswith("."):
                                continue
                            Storage._validate_id(directory.name)
                            if not directory.is_dir():
                                raise ValueError("游戏资源必须是目录")
                            archive.write(directory, f"games/{directory.name}")
                            for parent, directories, files in os.walk(directory, followlinks=False, onerror=_raise_walk_error):
                                if Path(parent) == directory / "backups":
                                    directories[:] = [name for name in directories if not name.startswith(".")]
                                for name in sorted(directories + files):
                                    path = Path(parent) / name
                                    archive.write(path, f"games/{path.relative_to(root).as_posix()}")
                with zipfile.ZipFile(temporary) as archive:
                    _, resources = self._library_header(archive)
                for game_id, backup_ids in resources.items():
                    directory = root / game_id
                    self._library_counter(directory)
                    self._validated_backup_records({"id": game_id}, directory, backup_ids)
                    self._artwork_manifest(directory / "artwork")
                temporary.rename(destination)
                return destination
            finally:
                temporary.unlink(missing_ok=True)

    def import_library(self, path: Path) -> dict:
        with self._lock, self.storage._lock:
            root = self.storage.data_dir / "games"
            if root.exists() or _is_link(root):
                self._local_tree(root)
            memory = copy.deepcopy((self.storage.games, self.storage.settings))
            temporary = previous = None
            moved = published = False
            try:
                with zipfile.ZipFile(path) as archive:
                    library, resources = self._library_header(archive)
                    routes, games, existing = self._library_routes(library, resources)
                    if archive.testzip() is not None:
                        raise ValueError("资料库包 CRC 校验失败，未导入任何内容")
                    temporary = self.storage.data_dir / f".library-import-{uuid.uuid4().hex}"
                    incoming = temporary / "incoming"
                    incoming.mkdir(parents=True)
                    for member in archive.infolist():
                        output = incoming.joinpath(*member.filename.rstrip("/").split("/"))
                        if member.is_dir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, output.open("xb") as target:
                                shutil.copyfileobj(source, target)
                staged = Storage(temporary / "merged")
                staged.replace_library({"games": games, "settings": {**self.storage.settings, **library["settings"]}})
                staged_root = staged.data_dir / "games"
                if root.exists():
                    shutil.copytree(root, staged_root, symlinks=True)
                    self._local_tree(staged_root)
                else:
                    staged_root.mkdir()
                manager = BackupManager(staged)
                imported = skipped = 0
                for source_id, backup_ids in resources.items():
                    source = incoming / "games" / source_id
                    game = {"id": routes[source_id]}
                    merged = staged.game_dir(game)
                    merged.mkdir(exist_ok=True)
                    counter = max(self._library_counter(source), self._library_counter(merged))
                    atomic_write_json(merged / "sequence.json", {"next_sequence": counter})
                    added, ignored = manager._import_backups({"id": source_id}, game, source, merged, backup_ids)
                    imported += added
                    skipped += ignored
                    manager._import_artwork(source, merged)
                self._local_tree(staged_root)
                previous = self.storage.data_dir / f".library-previous-{uuid.uuid4().hex}"
                if root.exists():
                    root.rename(previous)
                    moved = True
                staged_root.rename(root)
                published = True
                self.storage.replace_library({"games": staged.games, "settings": staged.settings})
            except Exception as error:
                self.storage.games, self.storage.settings = memory
                try:
                    if published:
                        root.rename(temporary / "failed")
                    if moved:
                        previous.rename(root)
                except OSError as rollback_error:
                    location = previous if moved else root
                    raise RuntimeError(f"资料库导入失败且回滚未完成，保留的原游戏资源位于 {location}，请手动恢复。") from rollback_error
                if isinstance(error, zipfile.BadZipFile):
                    raise ValueError("资料库包或内部备份 ZIP 已损坏，未导入任何内容") from error
                raise
            else:
                source_ids = {game["id"] for game in library["games"]}
                result = {"new_games": len(source_ids) - existing, "merged_games": existing,
                          "imported": imported, "skipped": skipped, "orphan_count": len(set(resources) - source_ids),
                          "settings_keys": list(library["settings"])}
                if moved:
                    try:
                        shutil.rmtree(previous)
                    except OSError:
                        result["cleanup_warning"] = f"导入成功，但原资料库资源目录未能清理：{previous}"
                return result
            finally:
                if temporary is not None and temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
