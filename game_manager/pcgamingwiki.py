"""通过 PCGamingWiki 官方 MediaWiki API 查询 Windows 存档目录。"""

from __future__ import annotations

import ntpath
import re
from html.parser import HTMLParser
from urllib.parse import quote

import requests

from .network import NetworkError, create_session


class PCGamingWikiError(NetworkError):
    """可直接展示给用户的存档查询错误。"""


class _SaveTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._heading = None
        self._heading_text = []
        self._target = False
        self._active = False
        self._table_depth = 0
        self._row = []
        self._cell = None
        self._skip = []
        self._previous_label = ""
        self._remaining_rows = 0
        self._path_depth = 0
        self._path_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._skip or tag in ("sup", "script", "style") or "mw-editsection" in attrs.get("class", "").split():
            if tag not in ("br", "img", "hr", "input", "meta", "link", "wbr"):
                self._skip.append(tag)
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._heading = tag
            self._heading_text = []
            self._target = False
            self._active = False
        if attrs.get("id", "").casefold() == "save_game_data_location":
            self._target = True
        if tag == "table" and (self._active or self._table_depth):
            self._table_depth += 1
        if self._table_depth == 1:
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th"):
                try:
                    span = max(1, min(20, int(attrs.get("rowspan", "1"))))
                except ValueError:
                    span = 1
                self._cell = {"text": [], "span": span, "paths": []}
        if self._path_depth and tag not in ("br", "img", "hr", "wbr"):
            self._path_depth += 1
        elif self._cell is not None and (tag == "code" or (tag == "span" and "monospace" in attrs.get("class", "").split())):
            self._path_depth = 1
            self._path_text = []
        if self._cell is not None and tag in ("br", "p", "div", "li"):
            self._cell["text"].append("\n")
            if self._path_depth:
                self._path_text.append("\n")

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if self._path_depth:
            self._path_depth -= 1
            if not self._path_depth and self._cell is not None:
                self._cell["paths"].append("".join(self._path_text))
        if tag == self._heading:
            heading = " ".join("".join(self._heading_text).split()).casefold()
            self._active = self._target or heading == "save game data location"
            self._heading = None
        if self._cell is not None and tag in ("p", "div", "li"):
            self._cell["text"].append("\n")
        if self._table_depth == 1:
            if tag in ("td", "th") and self._cell is not None:
                self._row.append(self._cell)
                self._cell = None
            elif tag == "tr":
                if len(self._row) >= 2:
                    label = " ".join("".join(self._row[0]["text"]).split())
                    self._previous_label = label
                    self._remaining_rows = self._row[0]["span"] - 1
                    self.rows.append((label, "\n".join(self._row[1]["paths"]) if self._row[1]["paths"] else "".join(self._row[1]["text"])))
                elif len(self._row) == 1 and self._remaining_rows:
                    self.rows.append((self._previous_label, "\n".join(self._row[0]["paths"]) if self._row[0]["paths"] else "".join(self._row[0]["text"])))
                    self._remaining_rows -= 1
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if not self._table_depth:
                self._active = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._heading is not None:
            self._heading_text.append(data)
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._path_depth:
                self._path_text.append(data)


def _windows_label(label: str) -> bool:
    folded = label.casefold()
    if any(system in folded for system in ("linux", "macos", "os x", "osx", "proton")):
        return False
    return "windows" in folded or folded in {"steam", "gog", "gog.com", "epic games launcher", "epic games store",
                                               "microsoft store", "xbox app", "uplay", "ubisoft connect", "origin", "ea app", "battle.net"}


def _directory_candidate(raw: str) -> tuple[str, bool]:
    path = raw.strip().replace("\xa0", " ")
    file_part = ntpath.basename(path)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    invalid_file = file_part.split(".")[0].upper() in reserved or bool(re.search(r'[<>"|:\x00-\x1f]', file_part))
    is_file = not path.endswith(("\\", "/")) and bool(re.search(r"\.(?:sav|save|dat|bin|json|xml|db|sqlite|rfo|profile|vdf|ini|txt|sl2|ess|fos|srm|sol|rpgsave)$", file_part, re.I))
    ambiguous_file = not path.endswith(("\\", "/")) and "." in file_part and not is_file
    if is_file:
        path = ntpath.dirname(path)
    path = path.rstrip("\\/")
    parts = re.split(r"[\\/]", path)
    variables = re.findall(r"%([^%]+)%", path)
    known = {"USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PUBLIC", "ALLUSERSPROFILE", "HOMEDRIVE", "HOMEPATH"}
    absolute = bool(re.match(r"^(?:[A-Za-z]:[\\/]|%[A-Za-z_]+%[\\/])", path))
    safe = (absolute and not ambiguous_file and not invalid_file and not re.search(r'[<>"|?*\x00-\x1f]', path)
            and not any(part in ("", ".", "..") for part in parts[1:])
            and not any(variable.upper() not in known for variable in variables)
            and "%" not in re.sub(r"%[^%]+%", "", path)
            and not re.search(r":", path[2:] if re.match(r"^[A-Za-z]:", path) else path)
            and not any(part.endswith((" ", ".")) or part.split(".")[0].upper() in reserved for part in parts))
    broad = {"%userprofile%", "%appdata%", "%localappdata%", "%programdata%", "%public%", "%allusersprofile%",
             "%userprofile%\\documents", "%userprofile%\\saved games", "%userprofile%\\appdata",
             "%userprofile%\\appdata\\locallow", "%userprofile%\\appdata\\local", "%userprofile%\\appdata\\roaming",
             "%userprofile%\\documents\\my games"}
    normalized = path.replace("/", "\\").casefold()
    safe = safe and normalized not in broad and not re.fullmatch(r"[a-z]:", normalized)
    safe = safe and not re.fullmatch(r"[a-z]:\\(?:users(?:\\[^\\]+(?:\\(?:documents(?:\\my games)?|saved games|appdata(?:\\(?:local|locallow|roaming))?))?)?|windows|program files(?: \(x86\))?|programdata)", normalized)
    return path, bool(safe)


class PCGamingWiki:
    BASE_URL = "https://www.pcgamingwiki.com/w/api.php"
    TIMEOUT = (5, 20)
    USER_AGENT = "GameSaveManager/1.0 (desktop save manager; PCGamingWiki MediaWiki API)"

    def __init__(self, proxy_url: str = ""):
        self._session = create_session(proxy_url)

    def close(self) -> None:
        self._session.close()

    def _request(self, **params) -> dict:
        try:
            response = self._session.get(self.BASE_URL, params={"format": "json", "formatversion": 2, **params},
                                         headers={"User-Agent": self.USER_AGENT, "Accept": "application/json"}, timeout=self.TIMEOUT)
            if response.status_code == 403:
                raise PCGamingWikiError("PCGamingWiki 拒绝访问（HTTP 403），请稍后重试或手动查看网站填写存档目录。")
            if response.status_code == 429:
                raise PCGamingWikiError("PCGamingWiki 请求过于频繁（HTTP 429），请等待至少 60 秒后重试。")
            if response.status_code != 200:
                raise PCGamingWikiError(f"PCGamingWiki 请求失败（HTTP {response.status_code}）。")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("响应不是对象")
            return value
        except requests.Timeout:
            raise PCGamingWikiError("PCGamingWiki 查询超时，请检查网络或 HTTP 代理设置。") from None
        except ValueError:
            raise PCGamingWikiError("PCGamingWiki 返回了无效数据，无法识别存档目录。") from None
        except requests.RequestException:
            raise PCGamingWikiError("无法连接 PCGamingWiki，请检查网络或 HTTP 代理设置。") from None

    def find_save_locations(self, english_name: str) -> list[dict]:
        if not isinstance(english_name, str) or not english_name.strip():
            raise PCGamingWikiError("请先填写游戏英文名。")
        name = english_name.strip()
        data = self._request(action="parse", page=name, prop="text", redirects=1)
        error = data.get("error", {})
        if not isinstance(error, dict):
            raise PCGamingWikiError("PCGamingWiki 返回了无效的文章数据。")
        if error.get("code") == "missingtitle":
            search = self._request(action="query", list="search", srsearch=name, srnamespace=0, srlimit=5)
            query = search.get("query", {})
            matches = query.get("search", []) if isinstance(query, dict) else None
            if not isinstance(matches, list):
                raise PCGamingWikiError("PCGamingWiki 返回了无效的文章搜索结果。")
            exact = next((item.get("title") for item in matches if isinstance(item, dict)
                          and isinstance(item.get("title"), str) and item["title"].casefold() == name.casefold()), None)
            if exact is None:
                raise PCGamingWikiError("PCGamingWiki 没有找到精确匹配的游戏文章，请核对英文名或手动填写存档目录。")
            data = self._request(action="parse", page=exact, prop="text", redirects=1)
        try:
            parsed = data["parse"]
            title, html = parsed["title"], parsed["text"]
            if not isinstance(title, str) or not title or not isinstance(html, str):
                raise ValueError("文章响应无效")
        except (KeyError, TypeError, ValueError):
            raise PCGamingWikiError("PCGamingWiki 无法提供该游戏文章，无法识别存档目录。") from None
        parser = _SaveTableParser()
        parser.feed(html)
        parser.close()
        candidates, seen = [], set()
        for label, text in parser.rows:
            if not _windows_label(label):
                continue
            for raw in text.splitlines():
                raw = raw.strip()
                if not raw or not re.match(r"^(?:%|[A-Za-z]:[\\/]|<|HK(?:EY_|CU\\|LM\\|CR\\|U\\|CC\\))", raw, re.I):
                    continue
                path, resolved = _directory_candidate(raw)
                key = (label.casefold(), path.casefold())
                if key in seen:
                    continue
                seen.add(key)
                display_label = label + "（文件所在目录）" if path != raw.rstrip("\\/") else label
                candidates.append({"title": title, "path": path, "label": display_label,
                                   "page_url": "https://www.pcgamingwiki.com/wiki/" + quote(title.replace(" ", "_"), safe=""),
                                   "resolved": resolved})
        return sorted(candidates, key=lambda item: not item["resolved"])


def pcgw_worker(connection, proxy_url: str, english_name: str) -> None:
    client = None
    try:
        client = PCGamingWiki(proxy_url)
        connection.send((True, client.find_save_locations(english_name)))
    except NetworkError as error:
        connection.send((False, str(error)))
    except Exception:
        connection.send((False, "存档目录查询失败，请稍后重试或手动填写。"))
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            connection.close()
