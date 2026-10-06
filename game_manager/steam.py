"""通过 Steam 商店接口将 App ID 查询为英文游戏名。"""

from __future__ import annotations

import requests

from .network import NetworkError, create_session


class SteamError(NetworkError):
    """可直接展示给用户的 Steam 游戏名查询错误。"""


def lookup_steam_name(app_id: str, proxy_url: str = "") -> str:
    if not isinstance(app_id, str) or not app_id.strip().isdecimal():
        raise SteamError("请填写有效的 Steam App ID（大于 0 的纯数字）。")
    try:
        number = int(app_id.strip())
    except ValueError:
        raise SteamError("请填写有效的 Steam App ID（大于 0 的纯数字）。") from None
    if number <= 0:
        raise SteamError("请填写有效的 Steam App ID（大于 0 的纯数字）。")
    app_id = str(number)
    try:
        session = create_session(proxy_url)
    except NetworkError as error:
        raise SteamError(str(error)) from None
    response = None
    try:
        response = session.get("https://store.steampowered.com/api/appdetails",
                               params={"appids": app_id, "l": "english"},
                               headers={"User-Agent": "GameSaveManager/1.0", "Accept": "application/json"},
                               timeout=(5, 15))
        if response.status_code != 200:
            raise SteamError(f"Steam 游戏名查询失败（HTTP {response.status_code}）。")
        value = response.json()
        application = value.get(app_id) if isinstance(value, dict) else None
        if not isinstance(application, dict):
            raise ValueError("应用响应无效")
        if application.get("success") is False:
            raise SteamError("Steam 未找到该 App ID 对应的游戏，请检查编号或手动填写英文名。")
        data = application.get("data")
        if (application.get("success") is not True or not isinstance(data, dict)
                or type(data.get("steam_appid")) is not int or data["steam_appid"] != number
                or not isinstance(data.get("name"), str) or not data["name"].strip()):
            raise ValueError("应用信息无效")
        return data["name"].strip()
    except requests.Timeout:
        raise SteamError("Steam 游戏名查询超时，请检查网络或代理设置。") from None
    except ValueError:
        raise SteamError("Steam 返回了无效数据，无法识别游戏英文名。") from None
    except requests.RequestException:
        raise SteamError("无法连接 Steam，请检查网络或代理设置。") from None
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            session.close()


def steam_name_worker(connection, proxy_url: str, app_id: str) -> None:
    try:
        connection.send((True, lookup_steam_name(app_id, proxy_url)))
    except NetworkError as error:
        connection.send((False, str(error)))
    except Exception:
        connection.send((False, "Steam 游戏名查询失败，请稍后重试或手动填写英文名。"))
    finally:
        connection.close()
