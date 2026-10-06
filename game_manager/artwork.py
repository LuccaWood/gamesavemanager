"""使用 SteamGridDB 官方 API 下载经过校验的游戏装饰图片。"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import warnings
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from PIL import Image

from .network import NetworkError, create_session, validate_proxy_url


class ArtworkError(NetworkError):
    """可直接展示给用户的图片获取错误。"""


_ASSET_SPECS = {
    "cover": ("grids", (600, 900)), "wide": ("grids", (920, 430)),
    "hero": ("heroes", (1920, 620)), "logo": ("logos", None),
}


def _asset_spec(kind: str):
    if kind not in _ASSET_SPECS:
        raise ArtworkError("图片类型无效。")
    return _ASSET_SPECS[kind]


def _ordinary_directory(destination: Path) -> Path:
    destination = Path(destination).absolute()
    if destination.parent == destination:
        raise ArtworkError("图片目录不能是磁盘根目录。")
    if (destination.is_symlink() or destination.is_junction()
            or (destination.exists() and not destination.is_dir())):
        raise ArtworkError("图片存储位置必须是普通文件夹。")
    return destination


def _image_info(path: Path, formats: tuple, size=None, kind=None, local=False):
    label = "选择" if local else "下载"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in formats:
                    raise ArtworkError(f"{label}的图片格式不符合要求。")
                if getattr(image, "is_animated", False):
                    raise ArtworkError(f"{label}的图片是动态图，需要静态图片。")
                if width * height > SteamGridDB.MAX_IMAGE_PIXELS:
                    raise ArtworkError("图片像素数量过大，已停止处理。")
                if size is not None and (width, height) != size:
                    title = {"cover": "封面", "wide": "横版大图", "hero": "标题背景", "logo": "标志"}[kind]
                    raise ArtworkError(f"下载的{title}尺寸不正确，需要 {size[0]}×{size[1]}。")
                image.verify()
            with Image.open(path) as image:
                image.load()
        return image_format, width, height
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ArtworkError(f"{label}的图片已损坏或无法读取。") from None


def _read_manifest(directory: Path) -> dict:
    path = directory / "assets.json"
    if not path.exists():
        return {"assets": {}, "missing": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("assets"), dict):
            raise ValueError
        for asset in value["assets"].values():
            if not isinstance(asset, dict) or not isinstance(asset.get("file"), str):
                raise ValueError
            filename = asset["file"]
            if not filename or filename in (".", "..") or Path(filename).name != filename or "\\" in filename:
                raise ValueError
        if not isinstance(value.get("missing", []), list) or any(
                not isinstance(item, str) for item in value.get("missing", [])):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ArtworkError("图片来源记录损坏，请重新获取图片。") from None


def merge_assets(staging: Path, destination: Path) -> dict:
    """将单类暂存图片与现有其它图片合并，只写入暂存目录。"""
    staging, destination = _ordinary_directory(staging), _ordinary_directory(destination)
    for directory in (staging, destination):
        if directory.exists():
            for path in directory.iterdir():
                if path.is_symlink() or path.is_junction():
                    raise ArtworkError("图片目录包含链接，请先移除链接后重试。")
                if not path.is_file():
                    raise ArtworkError("图片目录只能包含普通图片和来源记录文件。")
    selected = _read_manifest(staging)
    if len(selected["assets"]) != 1:
        raise ArtworkError("单张图片更新内容无效。")
    kind, asset = next(iter(selected["assets"].items()))
    _asset_spec(kind)
    if not (staging / asset["file"]).is_file():
        raise ArtworkError("待更新的图片文件不存在。")
    previous = _read_manifest(destination)
    other_files = {record["file"].casefold() for key, record in previous["assets"].items() if key != kind}
    filename = asset["file"]
    if filename.casefold() in other_files:
        replacement = f"{kind}-{uuid.uuid4().hex}{Path(filename).suffix}"
        (staging / filename).rename(staging / replacement)
        asset = {**asset, "file": replacement}
        filename = replacement
    old_file = previous["assets"].get(kind, {}).get("file")
    old_names = {name.casefold() for name in (old_file, *(f"{kind}.{extension}" for extension in ("png", "jpg", "jpeg", "webp")))
                 if isinstance(name, str)}
    if destination.exists():
        for path in destination.iterdir():
            if path.name.casefold() in ("assets.json", filename.casefold()):
                continue
            if path.name.casefold() in old_names and path.name.casefold() not in other_files:
                continue
            shutil.copy2(path, staging / path.name)
    manifest = {**previous, **{key: value for key, value in selected.items() if key not in ("assets", "missing")}}
    manifest["assets"] = {**previous["assets"], kind: asset}
    manifest["missing"] = [item for item in previous.get("missing", []) if item != kind]
    (staging / "assets.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def replace_local_asset(kind: str, source: Path, destination: Path) -> dict:
    """校验并复制用户选择的原始图片，保留其余类型的图片。"""
    _asset_spec(kind)
    destination = _ordinary_directory(destination)
    source = Path(source)
    staging = None
    try:
        if not source.is_file():
            raise ArtworkError("选择的图片文件不存在。")
        if source.stat().st_size > SteamGridDB.MAX_IMAGE_BYTES:
            raise ArtworkError("图片文件过大，已停止复制。")
        image_format, width, height = _image_info(source, ("PNG", "JPEG", "WEBP"), local=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        filename = f"{kind}.{ {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}[image_format]}"
        shutil.copy2(source, staging / filename)
        manifest = {"assets": {kind: {"file": filename, "width": width, "height": height, "source": "local"}},
                    "missing": []}
        (staging / "assets.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = merge_assets(staging, destination)
        SteamGridDB._replace_directory(staging, destination)
        return result
    except OSError:
        raise ArtworkError("无法写入图片目录，请检查磁盘空间和目录权限。") from None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


class SteamGridDB:
    BASE_URL = "https://www.steamgriddb.com/api/v2"
    TIMEOUT = (5, 30)
    MAX_IMAGE_BYTES = 25 * 1024 * 1024
    MAX_IMAGE_PIXELS = 40_000_000
    USER_AGENT = "GameSaveManager/1.0"

    def __init__(self, api_key: str, proxy_url: str = ""):
        self._api_key = api_key.strip()
        self._session = create_session(self.validate_proxy_url(proxy_url))

    @staticmethod
    def validate_proxy_url(value: str) -> str:
        try:
            return validate_proxy_url(value)
        except NetworkError as error:
            raise ArtworkError(str(error)) from None

    def close(self) -> None:
        self._session.close()

    def search(self, english_name: str) -> list[dict]:
        name = english_name.strip()
        if not name:
            raise ArtworkError("请先填写游戏英文名。")
        data = self._api_data(f"/search/autocomplete/{quote(name, safe='')}")
        games = []
        for item in data:
            if not self._valid_id(item.get("id")) or not isinstance(item.get("name"), str):
                raise ArtworkError("SteamGridDB 返回的游戏信息不完整。")
            games.append({
                "id": item["id"],
                "name": self._redact(item["name"]),
                "release_date": item.get("release_date"),
            })
        return games

    def gallery(self, game_id: int, kind: str, destination: Path, page: int = 0) -> dict:
        endpoint, size = _asset_spec(kind)
        if not self._valid_id(game_id):
            raise ArtworkError("游戏 ID 必须是正整数。")
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            raise ArtworkError("图片页码无效。")
        destination = _ordinary_directory(destination)
        params = {"types": "static", "mimes": "image/png" if kind == "logo" else "image/png,image/jpeg",
                  "nsfw": "false", "humor": "false", "epilepsy": "false", "limit": 20, "page": page}
        if size is not None:
            params["dimensions"] = f"{size[0]}x{size[1]}"
        data = self._api_data(f"/{endpoint}/game/{game_id}", params)
        candidates = []
        try:
            destination.mkdir(parents=True, exist_ok=True)
            for item in data[:20]:
                if not self._valid_id(item.get("id")) or not isinstance(item.get("url"), str):
                    raise ArtworkError("SteamGridDB 返回的图片信息不完整。")
                if size is not None and "width" in item and "height" in item and (item["width"], item["height"]) != size:
                    continue
                if item.get("mime") and item["mime"] not in params["mimes"].split(","):
                    continue
                author = item.get("author") if isinstance(item.get("author"), dict) else {}
                candidate = {"id": item["id"], "url": self._validate_image_url(item["url"]),
                             "width": item.get("width"), "height": item.get("height"), "mime": item.get("mime"),
                             "author": {"name": self._redact(str(author.get("name", ""))),
                                        "steam64": self._redact(str(author.get("steam64", "")))}, "preview_file": ""}
                if isinstance(item.get("thumb"), str):
                    try:
                        thumbnail = self._validate_image_url(item["thumb"])
                        filename, _, _ = self._download(thumbnail, destination, f"preview-{item['id']}", None,
                                                        formats=("PNG", "JPEG", "WEBP"))
                        candidate["preview_file"] = filename
                    except ArtworkError:
                        (destination / f".preview-{item['id']}.download").unlink(missing_ok=True)
                candidates.append(candidate)
        except OSError:
            raise ArtworkError("无法写入图片预览，请检查磁盘空间和目录权限。") from None
        return {"game_id": game_id, "kind": kind, "page": page, "has_next": len(data) >= 20, "candidates": candidates}

    def download_asset(self, game_id: int, kind: str, candidate: dict, destination: Path) -> dict:
        _, size = _asset_spec(kind)
        if not self._valid_id(game_id):
            raise ArtworkError("游戏 ID 必须是正整数。")
        if (not isinstance(candidate, dict) or not self._valid_id(candidate.get("id"))
                or not isinstance(candidate.get("url"), str)):
            raise ArtworkError("SteamGridDB 返回的图片信息不完整。")
        url = self._validate_image_url(candidate["url"])
        destination = _ordinary_directory(destination)
        staging = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
            filename, width, height = self._download(url, staging, kind, size)
            author = candidate.get("author") if isinstance(candidate.get("author"), dict) else {}
            manifest = {"game_id": game_id, "assets": {kind: {
                "file": filename, "width": width, "height": height, "source_url": url, "id": candidate["id"],
                "author": {"name": self._redact(str(author.get("name", ""))),
                           "steam64": self._redact(str(author.get("steam64", "")))}}}, "missing": []}
            (staging / "assets.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            self._replace_directory(staging, destination)
            return manifest
        except OSError:
            raise ArtworkError("无法写入图片目录，请检查磁盘空间和目录权限。") from None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def download_assets(self, game_id: int, destination: Path) -> dict:
        if not self._valid_id(game_id):
            raise ArtworkError("游戏 ID 必须是正整数。")
        destination = Path(destination).absolute()
        if destination.parent == destination:
            raise ArtworkError("图片目录不能是磁盘根目录。")
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise ArtworkError("图片存储位置必须是普通文件夹。")
        specs = (
            ("cover", "grids", (600, 900)),
            ("wide", "grids", (920, 430)),
            ("hero", "heroes", (1920, 620)),
            ("logo", "logos", None),
        )
        manifest = {"game_id": game_id, "assets": {}, "missing": []}
        staging = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
            for kind, endpoint, size in specs:
                params = {
                    "types": "static", "mimes": "image/png" if kind == "logo" else "image/png,image/jpeg",
                    "nsfw": "false", "humor": "false", "epilepsy": "false", "limit": 5, "page": 0,
                }
                if size is not None:
                    params["dimensions"] = f"{size[0]}x{size[1]}"
                candidates = self._api_data(f"/{endpoint}/game/{game_id}", params)
                if not candidates:
                    manifest["missing"].append(kind)
                    continue
                candidate = None
                for item in candidates[:5]:
                    if not self._valid_id(item.get("id")) or not isinstance(item.get("url"), str):
                        raise ArtworkError("SteamGridDB 返回的图片信息不完整。")
                    if size is not None and "width" in item and "height" in item:
                        if (item["width"], item["height"]) != size:
                            continue
                    if item.get("mime") and item["mime"] not in params["mimes"].split(","):
                        continue
                    candidate = item
                    break
                if candidate is None:
                    manifest["missing"].append(kind)
                    continue
                url = self._validate_image_url(candidate["url"])
                filename, width, height = self._download(url, staging, kind, size)
                author = candidate.get("author")
                if not isinstance(author, dict):
                    author = {}
                manifest["assets"][kind] = {
                    "file": filename, "width": width, "height": height, "source_url": url,
                    "author": {
                        "name": self._redact(str(author.get("name", ""))),
                        "steam64": self._redact(str(author.get("steam64", ""))),
                    },
                    "id": candidate["id"],
                }
            (staging / "assets.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            self._replace_directory(staging, destination)
            return manifest
        except OSError:
            raise ArtworkError("无法写入图片目录，请检查磁盘空间和目录权限。") from None
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def _api_data(self, endpoint: str, params: dict | None = None) -> list[dict]:
        if not self._api_key:
            raise ArtworkError("请先在设置中填写 SteamGridDB API Key。")
        response = None
        try:
            response = self._session.get(
                self.BASE_URL + endpoint,
                headers={"Authorization": f"Bearer {self._api_key}", "User-Agent": self.USER_AGENT},
                params=params, timeout=self.TIMEOUT, allow_redirects=False,
            )
            self._check_status(response.status_code, api=True)
            try:
                payload = response.json()
            except ValueError:
                raise ArtworkError("SteamGridDB 返回了无法解析的数据，请稍后重试。") from None
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise ArtworkError("SteamGridDB 请求失败，请检查游戏名称或稍后重试。")
            data = payload.get("data")
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ArtworkError("SteamGridDB 返回的数据格式不正确。")
            return data
        except requests.Timeout:
            raise ArtworkError("连接 SteamGridDB 超时，请检查网络后重试。") from None
        except requests.exceptions.ProxyError:
            raise ArtworkError("无法连接代理，请检查地址和代理服务。") from None
        except requests.RequestException:
            raise ArtworkError("无法连接 SteamGridDB，请检查网络后重试。") from None
        finally:
            if response is not None:
                response.close()

    def _download(self, url: str, staging: Path, kind: str, size: tuple[int, int] | None,
                  formats: tuple | None = None) -> tuple[str, int, int]:
        response = None
        temporary = staging / f".{kind}.download"
        try:
            response = self._session.get(
                url, headers={"User-Agent": self.USER_AGENT}, stream=True,
                timeout=self.TIMEOUT, allow_redirects=False,
            )
            self._check_status(response.status_code, api=False)
            declared_size = response.headers.get("Content-Length")
            if declared_size is not None:
                try:
                    if int(declared_size) > self.MAX_IMAGE_BYTES:
                        raise ArtworkError("图片文件过大，已停止下载。")
                except ValueError:
                    raise ArtworkError("图片服务器返回的文件大小无效。") from None
            total = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.MAX_IMAGE_BYTES:
                        raise ArtworkError("图片文件过大，已停止下载。")
                    output.write(chunk)
            formats = formats or (("PNG",) if kind == "logo" else ("PNG", "JPEG"))
            image_format, width, height = _image_info(temporary, formats, size, kind)
            filename = f"{kind}.{ {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}[image_format]}"
            temporary.rename(staging / filename)
            return filename, width, height
        except requests.Timeout:
            raise ArtworkError("图片下载超时，请检查网络后重试。") from None
        except requests.exceptions.ProxyError:
            raise ArtworkError("无法连接代理，请检查地址和代理服务。") from None
        except requests.RequestException:
            raise ArtworkError("图片下载失败，请检查网络后重试。") from None
        finally:
            if response is not None:
                response.close()

    def _validate_image_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").lower()
            if (
                parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)
                or not (hostname == "steamgriddb.com" or hostname.endswith(".steamgriddb.com"))
                or (self._api_key and self._api_key in url)
            ):
                raise ArtworkError("图片链接不是可信的 SteamGridDB HTTPS 地址。")
        except ValueError:
            raise ArtworkError("SteamGridDB 返回的图片链接无效。") from None
        return url

    @staticmethod
    def _check_status(status: int, api: bool) -> None:
        if status == 401 and api:
            raise ArtworkError("SteamGridDB API Key 缺失或无效，请检查设置。")
        if status == 429:
            raise ArtworkError("SteamGridDB 请求过于频繁，请稍后重试。")
        if status == 404 and api:
            raise ArtworkError("SteamGridDB 未找到该游戏，请重新搜索。")
        if not 200 <= status < 300:
            raise ArtworkError(f"{'SteamGridDB 请求' if api else '图片下载'}失败（HTTP {status}），请稍后重试。")

    @staticmethod
    def _valid_id(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def _redact(self, value: str) -> str:
        return value.replace(self._api_key, "[已隐藏]") if self._api_key else value

    @staticmethod
    def _replace_directory(staging: Path, destination: Path) -> None:
        previous = destination.with_name(f".{destination.name}-previous-{uuid.uuid4().hex}")
        had_previous = destination.exists()
        if had_previous:
            destination.rename(previous)
        try:
            staging.rename(destination)
        except OSError:
            if had_previous:
                try:
                    previous.rename(destination)
                except OSError:
                    raise ArtworkError(f"图片更新失败且回滚未完成，原图片保留在 {previous}，请手动恢复。") from None
            raise
        if had_previous:
            shutil.rmtree(previous, ignore_errors=True)


def artwork_worker(connection, api_key, proxy_url, action, value, destination=None) -> None:
    client = None
    try:
        try:
            client = SteamGridDB(api_key, proxy_url)
            if action == "search":
                result = client.search(value)
            elif action == "download":
                if destination is None:
                    raise ArtworkError("图片后台任务缺少目标目录。")
                result = client.download_assets(value, Path(destination))
            elif action in ("gallery", "download_one"):
                if destination is None or not isinstance(value, dict):
                    raise ArtworkError("图片后台任务缺少目标目录或选择信息。")
                if action == "gallery":
                    result = client.gallery(value.get("game_id"), value.get("kind"), Path(destination), value.get("page", 0))
                else:
                    result = client.download_asset(value.get("game_id"), value.get("kind"), value.get("candidate"), Path(destination))
            else:
                raise ArtworkError("图片后台任务类型无效。")
            message = (True, result)
        except ArtworkError as error:
            message = (False, str(error))
        except Exception:
            message = (False, "图片后台任务失败，请重试。")
        connection.send(message)
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            connection.close()
