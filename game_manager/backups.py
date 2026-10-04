"""目录存档的备份、完整还原和离线导出。"""

from __future__ import annotations

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
        if not name or name != member.orig_filename or name.startswith("/") or "\\" in name:
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

    def restore(self, game: dict, backup_id: str) -> dict | None:
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
                safety = self.create(game, reason="before_restore") if destination.exists() else None
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
                if moved:
                    try:
                        shutil.rmtree(previous)
                    except OSError:
                        if safety is not None:
                            safety["cleanup_warning"] = f"还原成功，但旧目录未能清理：{previous}"
                return safety
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
