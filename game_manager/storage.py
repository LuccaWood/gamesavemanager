"""游戏条目和设置的本地持久化。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path


def atomic_write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.data_dir / "library.json"
        self._lock = threading.RLock()
        self.games: list[dict] = []
        self.settings: dict = {}
        if self._path.exists():
            try:
                value = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("数据必须是对象")
                games, settings = value["games"], value["settings"]
                if not isinstance(games, list) or not isinstance(settings, dict):
                    raise ValueError("games/settings 类型错误")
                ids, names = set(), set()
                for game in games:
                    if not isinstance(game, dict):
                        raise ValueError("游戏条目必须是对象")
                    self._validate_id(game["id"])
                    name = game["english_name"]
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("游戏英文名不能为空")
                    for key in ("chinese_name", "game_path", "save_path"):
                        if not isinstance(game[key], str):
                            raise ValueError(f"{key} 必须是文本")
                    if not isinstance(game["steamgrid_id"], (str, int)):
                        raise ValueError("steamgrid_id 类型错误")
                    if game["id"] in ids or name.strip().casefold() in names:
                        raise ValueError("存在重复游戏条目")
                    ids.add(game["id"])
                    names.add(name.strip().casefold())
                self.games, self.settings = games, settings
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"数据文件损坏：{self._path}。请保留文件并恢复备份。") from error

    @staticmethod
    def _validate_id(game_id: str) -> None:
        if not isinstance(game_id, str) or len(game_id) != 32:
            raise ValueError("游戏 ID 无效")
        try:
            if uuid.UUID(hex=game_id).hex != game_id:
                raise ValueError("游戏 ID 无效")
        except (ValueError, AttributeError) as error:
            raise ValueError("游戏 ID 无效") from error

    def _save(self, games: list[dict], settings: dict) -> None:
        atomic_write_json(self._path, {"games": games, "settings": settings})
        self.games, self.settings = games, settings

    def get_game(self, game_id: str) -> dict:
        with self._lock:
            for game in self.games:
                if game["id"] == game_id:
                    return dict(game)
        raise ValueError("游戏条目不存在")

    def save_game(self, values: dict) -> dict:
        with self._lock:
            name = values.get("english_name", "")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("请填写游戏英文名")
            name = name.strip()
            game_id = values.get("id") or uuid.uuid4().hex
            self._validate_id(game_id)
            existing = self.get_game(game_id) if values.get("id") else {}
            if any(game["id"] != game_id and game["english_name"].strip().casefold() == name.casefold()
                   for game in self.games):
                raise ValueError("已存在同名游戏（英文名不区分大小写）")
            game = {"id": game_id, "english_name": name}
            for key in ("chinese_name", "game_path", "save_path", "steamgrid_id"):
                value = values.get(key, existing.get(key, ""))
                if value is None:
                    value = ""
                if not isinstance(value, (str, int)) or (isinstance(value, int) and key != "steamgrid_id"):
                    raise ValueError(f"{key} 必须是文本")
                game[key] = value
            games = [dict(item) for item in self.games]
            index = next((i for i, item in enumerate(games) if item["id"] == game_id), None)
            if index is None:
                games.append(game)
            else:
                games[index] = game
            self._save(games, dict(self.settings))
            return dict(game)

    def delete_game(self, game_id: str) -> None:
        with self._lock:
            self.get_game(game_id)
            self._save([dict(game) for game in self.games if game["id"] != game_id], dict(self.settings))

    def update_settings(self, values: dict) -> None:
        with self._lock:
            self._save([dict(game) for game in self.games], {**self.settings, **values})

    def game_dir(self, game: dict) -> Path:
        self._validate_id(game["id"])
        return self.data_dir / "games" / game["id"]

    def artwork_dir(self, game: dict) -> Path:
        return self.game_dir(game) / "artwork"
