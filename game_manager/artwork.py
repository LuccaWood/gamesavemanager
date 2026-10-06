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
            raise ArtworkError("无法连接 HTTP 代理，请检查地址和代理服务。") from None
        except requests.RequestException:
            raise ArtworkError("无法连接 SteamGridDB，请检查网络后重试。") from None
        finally:
            if response is not None:
                response.close()

    def _download(self, url: str, staging: Path, kind: str, size: tuple[int, int] | None) -> tuple[str, int, int]:
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
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(temporary) as image:
                        image_format = image.format
                        width, height = image.size
                        if image_format not in (("PNG",) if kind == "logo" else ("PNG", "JPEG")):
                            raise ArtworkError("下载的图片格式不符合要求。")
                        if getattr(image, "is_animated", False):
                            raise ArtworkError("下载的图片是动态图，需要静态图片。")
                        if width * height > self.MAX_IMAGE_PIXELS:
                            raise ArtworkError("图片像素数量过大，已停止处理。")
                        if size is not None and (width, height) != size:
                            label = {"cover": "封面", "wide": "横版大图", "hero": "标题背景", "logo": "标志"}[kind]
                            raise ArtworkError(f"下载的{label}尺寸不正确，需要 {size[0]}×{size[1]}。")
                        image.verify()
                    with Image.open(temporary) as image:
                        image.load()
            except (OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise ArtworkError("下载的图片已损坏或无法读取。") from None
            filename = f"{kind}.{'png' if image_format == 'PNG' else 'jpg'}"
            temporary.rename(staging / filename)
            return filename, width, height
        except requests.Timeout:
            raise ArtworkError("图片下载超时，请检查网络后重试。") from None
        except requests.exceptions.ProxyError:
            raise ArtworkError("无法连接 HTTP 代理，请检查地址和代理服务。") from None
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
                previous.rename(destination)
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
