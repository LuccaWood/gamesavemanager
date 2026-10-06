"""查询第三方 SteamCMD API 提供的 Steam Auto-Cloud 存档规则。"""

from __future__ import annotations

import ctypes
import ntpath
import re
import sys
import uuid

import requests

from .network import NetworkError, create_session
from .pcgamingwiki import _directory_candidate


class SteamCloudError(NetworkError):
    """可直接展示给用户的 Steam 云存档查询错误。"""


_ROOTS = {
    "WinMyDocuments": ("FDD39AD0-238F-46AF-ADB4-6C85480369C7", "<documents>"),
    "WinAppDataLocal": ("F1B32785-6FBA-4FCF-9D55-7B8E7F157091", "%LOCALAPPDATA%"),
    "WinAppDataRoaming": ("3EB685DB-65F9-4CF6-A03A-E3EF65729F3D", "%APPDATA%"),
    "WinAppDataLocalLow": ("A520A1A4-1780-4FF6-BD18-167343C5AF16", "<local-low>"),
    "WinSavedGames": ("4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4", "<saved-games>"),
}


def _known_folder(root: str) -> str | None:
    class GUID(ctypes.Structure):
        _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    pointer = ctypes.c_wchar_p()
    try:
        folder_id = GUID.from_buffer_copy(uuid.UUID(_ROOTS[root][0]).bytes_le)
        shell = ctypes.WinDLL("shell32")
        ole = ctypes.WinDLL("ole32")
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree.restype = None
        try:
            if shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(pointer)) != 0:
                return None
            return pointer.value
        finally:
            if pointer:
                ole.CoTaskMemFree(ctypes.cast(pointer, ctypes.c_void_p))
    except (AttributeError, OSError, ValueError):
        return None


def _root_path(root: str) -> tuple[str, bool]:
    if root in _ROOTS:
        template = _ROOTS[root][1]
        if sys.platform == "win32":
            actual = _known_folder(root)
            return (actual, True) if actual else (template, False)
        return template, template.startswith("%")
    if root in ("AppInstallDir", "AppInstallDirectory"):
        return "<game-folder>", False
    return f"<{root}>", False


def _windows_platforms(platforms) -> bool:
    if platforms is None:
        return True
    if not isinstance(platforms, dict):
        return False
    return any(isinstance(value, str) and value.casefold().replace(" ", "") in
               ("windows", "all", "allos", "alloses") for value in platforms.values())


def _windows_override(root: str, overrides) -> tuple[str, bool]:
    if not isinstance(overrides, dict):
        return root, False
    matching = []
    for override in overrides.values():
        if not isinstance(override, dict):
            return root, False
        if override.get("root") != root:
            continue
        system = override.get("os")
        compare = override.get("oscompare", "=")
        if not isinstance(system, str) or compare != "=":
            return root, False
        if system.casefold() != "windows":
            continue
        matching.append(override)
    if not matching:
        return root, True
    if len(matching) != 1:
        return root, False
    override = matching[0]
    if any(key in override for key in ("pathtransforms", "addpath", "replacepath")):
        return root, False
    replacement = override.get("useinstead")
    return (replacement, True) if isinstance(replacement, str) and replacement else (root, False)


class SteamCloud:
    SEARCH_URL = "https://store.steampowered.com/api/storesearch"
    INFO_URL = "https://api.steamcmd.net/v1/info/"
    SOURCE = "Steam 云存档"
    TIMEOUT = (5, 20)

    def __init__(self, proxy_url: str = ""):
        self._session = create_session(proxy_url)

    def close(self) -> None:
        self._session.close()

    def _request(self, url: str, **kwargs) -> dict:
        response = None
        try:
            response = self._session.get(url, headers={"User-Agent": "GameSaveManager/1.0",
                                                       "Accept": "application/json"}, timeout=self.TIMEOUT, **kwargs)
            if response.status_code != 200:
                raise SteamCloudError(f"Steam 云存档查询失败（HTTP {response.status_code}），请稍后重试。")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("响应不是对象")
            return data
        except requests.Timeout:
            raise SteamCloudError("Steam 云存档查询超时，请检查网络或代理设置。") from None
        except ValueError:
            raise SteamCloudError("Steam 云存档返回了无效数据，无法识别存档目录。") from None
        except requests.RequestException:
            raise SteamCloudError("无法连接 Steam 云存档数据源，请检查网络或代理设置。") from None
        finally:
            if response is not None:
                response.close()

    def search_games(self, name: str) -> list[dict]:
        if not isinstance(name, str) or not name.strip():
            raise SteamCloudError("请先填写游戏英文名。")
        name = name.strip()
        data = self._request(self.SEARCH_URL, params={"term": name, "l": "english", "cc": "us"})
        items = data.get("items")
        if not isinstance(items, list):
            raise SteamCloudError("Steam 返回了无效的游戏搜索结果。")
        candidates, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                continue
            app_id, title = item.get("id"), item.get("name")
            if type(app_id) is not int or not 0 < app_id < 2**32 or app_id in seen:
                continue
            if not isinstance(title, str) or not title.strip():
                continue
            seen.add(app_id)
            candidates.append({"id": app_id, "name": title.strip(), "source": self.SOURCE,
                               "page_url": f"https://steamdb.info/app/{app_id}/ufs/"})
        return sorted(candidates, key=lambda item: item["name"].casefold() != name.casefold())[:10]

    def find_save_locations(self, name: str, app_id: int) -> list[dict]:
        if not isinstance(name, str) or not name.strip():
            raise SteamCloudError("请先填写游戏英文名。")
        if type(app_id) is not int or not 0 < app_id < 2**32:
            raise SteamCloudError("请选择有效的 Steam 游戏编号。")
        data = self._request(self.INFO_URL + str(app_id))
        info = data.get("data", {}).get(str(app_id)) if isinstance(data.get("data"), dict) else None
        common = info.get("common") if isinstance(info, dict) else None
        if (data.get("status") != "success" or not isinstance(common, dict)
                or not isinstance(common.get("name"), str) or not common["name"].strip()
                or not isinstance(common.get("type"), str) or common["type"].casefold() != "game"):
            raise SteamCloudError("Steam 云存档返回了无效的游戏资料，请重新选择游戏。")
        game_id = common.get("gameid", app_id)
        if type(game_id) not in (int, str) or str(game_id) != str(app_id):
            raise SteamCloudError("Steam 云存档返回的游戏编号不一致，请重新选择游戏。")
        ufs = info.get("ufs", {})
        if not isinstance(ufs, dict) or not isinstance(ufs.get("savefiles", {}), dict):
            raise SteamCloudError("Steam 云存档返回了无效的存档规则。")
        candidates, seen = [], set()
        for rule in ufs.get("savefiles", {}).values():
            if not isinstance(rule, dict) or not _windows_platforms(rule.get("platforms")):
                continue
            root, raw, pattern = rule.get("root"), rule.get("path"), rule.get("pattern")
            if not all(isinstance(value, str) for value in (root, raw, pattern)) or not root:
                continue
            root, override_valid = _windows_override(root, ufs.get("rootoverrides", {}))
            if root.startswith(("Mac", "Linux")) and override_valid:
                continue
            base, root_resolved = _root_path(root)
            relative = raw.replace("/", "\\")
            # Steam 配置常用一个前导 /；盘符、UNC 或反斜杠根路径不可作为相对目录。
            relative_safe = (not ntpath.splitdrive(raw)[0] and not raw.startswith(("//", "\\")))
            relative = relative.strip("\\")
            path = base + ("\\" + relative if relative else "")
            path, directory_safe = _directory_candidate(path + "\\")
            resolved = (root_resolved and override_valid and relative_safe and directory_safe and bool(relative)
                        and relative != "." and not re.search(r"[{}%]", relative)
                        and not (root == "WinMyDocuments" and relative.casefold() == "my games"))
            label = f"Windows 云同步规则（文件匹配：{pattern}）"
            if not override_valid:
                label += "；Windows 根目录覆盖规则需要核对"
            key = (path.casefold(), pattern.casefold(), bool(resolved))
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"title": common["name"].strip(), "path": path, "label": label,
                               "page_url": f"https://steamdb.info/app/{app_id}/ufs/", "resolved": bool(resolved),
                               "source": self.SOURCE})
        return sorted(candidates, key=lambda item: not item["resolved"])
