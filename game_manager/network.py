"""统一处理应用内网络代理与直连设置。"""

from __future__ import annotations

from urllib.parse import urlsplit

import requests


class NetworkError(RuntimeError):
    """可直接展示给用户的网络配置错误。"""


def validate_proxy_url(value: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError("代理地址必须是文本")
        value = value.strip()
        if not value:
            return ""
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("代理地址包含空白")
        parsed = urlsplit(value)
        scheme = "socks5h" if parsed.scheme == "socket" else parsed.scheme
        if (scheme not in ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
                or not parsed.hostname or parsed.port is None
                or not 1 <= parsed.port <= 65535 or parsed.path or parsed.query or parsed.fragment
                or parsed.netloc.count("@") > 1 or (parsed.password is not None and not parsed.username)):
            raise ValueError("代理地址结构无效")
        return f"{scheme}://{parsed.netloc}"
    except (TypeError, ValueError):
        raise NetworkError("代理地址无效，请使用 协议://主机:端口，支持 HTTP、HTTPS、SOCKS4/4a、SOCKS5/5h 和 socket。") from None


def create_session(proxy_url: str = "") -> requests.Session:
    proxy_url = validate_proxy_url(proxy_url)
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    return session


def settings_proxy_url(settings: dict) -> str:
    if settings.get("proxy_enabled") is not True:
        return ""
    proxy_url = validate_proxy_url(settings.get("proxy_url", ""))
    if not proxy_url:
        raise NetworkError("已启用代理，请填写代理地址。")
    return proxy_url
