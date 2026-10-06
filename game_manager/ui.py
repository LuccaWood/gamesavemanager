from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from . import configure_tk_runtime

configure_tk_runtime()

from tkinter import Frame, filedialog, messagebox, ttk
import webbrowser

import customtkinter as ctk
from PIL import Image, ImageOps

from .artwork import ArtworkError, artwork_worker, merge_assets, replace_local_asset
from .backups import BackupManager
from .network import NetworkError, settings_proxy_url, validate_proxy_url
from .pcgamingwiki import pcgw_worker
from .steam import steam_name_worker
from .storage import Storage


BG = "#11151e"
PANEL = "#1a2130"
MUTED = "#96a4b9"
ACCENT = "#5b83f6"
ARTWORK_TITLES = {"cover": "竖版封面", "wide": "横版大图", "hero": "标题背景", "logo": "游戏 Logo"}


def open_folder(path: Path) -> None:
    path = path.resolve()
    if not path.exists():
        raise ValueError("文件夹不存在，请先检查路径或完成操作。")
    if path.is_file():
        path = path.parent
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def local_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def readable_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


class GameDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp, game: dict | None = None):
        super().__init__(app)
        self.app = app
        self.game = game or {}
        self.steam_job = None
        self.steam_poll = None
        self.browse_buttons = []
        self.title("编辑游戏" if game else "添加游戏")
        self.geometry("660x480")
        self.resizable(False, False)
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text="游戏资料", font=ctk.CTkFont(size=23, weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=24, pady=(22, 12), sticky="w")
        self.entries = {}
        fields = [("english_name", "英文名 / App ID *"), ("chinese_name", "中文名"),
                  ("game_path", "游戏本体目录"), ("save_path", "存档目录")]
        for row, (key, label) in enumerate(fields, 1):
            ctk.CTkLabel(self, text=label).grid(row=row, column=0, padx=(24, 12), pady=10, sticky="w")
            entry = ctk.CTkEntry(self, height=38)
            entry.insert(0, self.game.get(key, ""))
            entry.grid(row=row, column=1, columnspan=1 if key.endswith("path") else 2,
                       padx=(0, 24), pady=10, sticky="ew")
            self.entries[key] = entry
            if key.endswith("path"):
                button = ctk.CTkButton(self, text="浏览…", width=74, command=lambda e=entry: self.browse(e))
                button.grid(row=row, column=2, padx=(0, 24), pady=10)
                self.browse_buttons.append(button)
        ctk.CTkLabel(self, text="英文名必填；纯数字按 Steam App ID 查询名称后保存。\n"
                     "存档目录留空时优先查询 PCGamingWiki，不可用时查询 Steam 云存档。",
                     text_color=MUTED, justify="left").grid(row=5, column=0, columnspan=3, padx=24, pady=8, sticky="w")
        self.error = ctk.CTkLabel(self, text="", text_color="#ff9d9d", wraplength=600)
        self.error.grid(row=6, column=0, columnspan=3, padx=24, sticky="w")
        self.save_button = ctk.CTkButton(self, text="保存游戏", height=40, command=self.save)
        self.save_button.grid(row=7, column=1, columnspan=2, padx=24, pady=18, sticky="e")
        self.bind("<Return>", lambda _: self.save())
        self.bind("<Escape>", lambda _: self.destroy())
        self.after(100, self.activate)

    def activate(self):
        self.grab_set()
        self.entries["english_name"].focus_set()

    def browse(self, entry):
        if self.steam_job is not None:
            return
        selected = filedialog.askdirectory(parent=self, title="选择目录")
        if selected:
            entry.delete(0, "end")
            entry.insert(0, selected)

    def save(self):
        if self.steam_job is not None or self.app._closing:
            return
        values = dict(self.game)
        values.update({key: entry.get().strip() for key, entry in self.entries.items()})
        if values["english_name"].isdecimal():
            self.resolve_steam_name(values)
            return
        self.save_values(values)

    def save_values(self, values):
        if values.get("english_name") != self.game.get("english_name"):
            values["steamgrid_id"] = None
        try:
            saved = self.app.storage.save_game(values)
        except (ValueError, OSError) as exc:
            self.error.configure(text=str(exc))
            return
        self.app.selected_id = saved["id"]
        self.app.refresh()
        self.destroy()
        self.app.lookup_save_path(saved)

    def set_lookup_state(self, querying):
        state = "disabled" if querying else "normal"
        for widget in list(self.entries.values()) + self.browse_buttons + [self.save_button]:
            widget.configure(state=state)
        self.save_button.configure(text="正在查询 Steam…" if querying else "保存游戏")

    def resolve_steam_name(self, values):
        receiver = sender = process = None
        try:
            proxy_url = settings_proxy_url(self.app.storage.settings)
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=steam_name_worker,
                                      args=(sender, proxy_url, values["english_name"]), daemon=True)
            process.start()
        except Exception as exc:
            if receiver is not None:
                receiver.close()
            if process is not None:
                process.close()
            self.error.configure(text=str(exc) if isinstance(exc, NetworkError) else "无法启动 Steam 查询，请重试。")
            return
        finally:
            if sender is not None:
                sender.close()
        self.steam_job = {"process": process, "receiver": receiver, "values": values, "proxy_url": proxy_url}
        self.set_lookup_state(True)
        self.error.configure(text="正在根据 Steam App ID 查询英文名称；关闭窗口可取消。")
        self.steam_poll = self.after(100, self.poll_steam_name)

    def poll_steam_name(self):
        self.steam_poll = None
        job = self.steam_job
        if job is None:
            return
        if self.app._closing:
            self.destroy()
            return
        if "result" not in job and job["receiver"].poll():
            try:
                job["result"] = job["receiver"].recv()
            except (EOFError, OSError):
                job["result"] = (False, "Steam 查询进程异常退出，请重试。")
        if job["process"].is_alive():
            self.steam_poll = self.after(100, self.poll_steam_name)
            return
        if "result" not in job and job["receiver"].poll():
            try:
                job["result"] = job["receiver"].recv()
            except (EOFError, OSError):
                job["result"] = (False, "Steam 查询进程异常退出，请重试。")
        success, result = job.get("result", (False, "Steam 查询进程异常退出，请重试。"))
        self.stop_steam_lookup()
        self.set_lookup_state(False)
        if not success:
            self.error.configure(text=result)
            return
        self.entries["english_name"].delete(0, "end")
        self.entries["english_name"].insert(0, result)
        self.save_values({**job["values"], "english_name": result})

    def stop_steam_lookup(self):
        if self.steam_poll is not None:
            self.after_cancel(self.steam_poll)
            self.steam_poll = None
        job, self.steam_job = self.steam_job, None
        if job is not None:
            process = job["process"]
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            else:
                process.join(timeout=0)
            job["receiver"].close()
            process.close()

    def destroy(self):
        self.stop_steam_lookup()
        super().destroy()


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp):
        super().__init__(app)
        self.app = app
        self.title("软件设置")
        self.geometry("590x560")
        self.resizable(False, False)
        self.transient(app)
        ctk.CTkLabel(self, text="软件设置", font=ctk.CTkFont(size=23, weight="bold")).pack(
            anchor="w", padx=24, pady=(24, 12))
        ctk.CTkLabel(self, text="SteamGridDB API Key：用于获取游戏装饰图片。", text_color=MUTED).pack(
            anchor="w", padx=24)
        self.key = ctk.CTkEntry(self, show="•", height=40)
        self.key.insert(0, app.api_key)
        self.key.pack(fill="x", padx=24, pady=14)
        self.remember = ctk.CTkCheckBox(self, text="记住 API Key（明文保存在本机，不进入单游戏包）")
        if app.storage.settings.get("api_key"):
            self.remember.select()
        self.remember.pack(anchor="w", padx=24)
        self.proxy_enabled = ctk.CTkCheckBox(self, text="为软件联网请求使用代理", command=self.toggle_proxy)
        if app.storage.settings.get("proxy_enabled") is True:
            self.proxy_enabled.select()
        self.proxy_enabled.pack(anchor="w", padx=24, pady=(20, 8))
        self.proxy_url = ctk.CTkEntry(self, height=38, placeholder_text="http://127.0.0.1:7890 或 https://代理主机:端口")
        self.proxy_url.insert(0, app.storage.settings.get("proxy_url", ""))
        self.proxy_url.pack(fill="x", padx=24)
        self.toggle_proxy()
        ctk.CTkLabel(self, text="仅支持 HTTP 和 HTTPS 代理。\n"
                     "用于 Steam、SteamGridDB、PCGamingWiki 和 SteamCMD；去除勾选后本机直连。", text_color=MUTED,
                     justify="left").pack(
            anchor="w", padx=24, pady=(6, 0))
        ctk.CTkButton(self, text="打开 API Key 申请页面 ↗", fg_color="transparent", border_width=1,
                      command=lambda: webbrowser.open("https://www.steamgriddb.com/profile/preferences")).pack(
            anchor="w", padx=24, pady=18)
        ctk.CTkButton(self, text="保存设置", command=self.save, height=38).pack(anchor="e", padx=24)
        self.after(100, self.grab_set)

    def toggle_proxy(self):
        self.proxy_url.configure(state="normal" if self.proxy_enabled.get() else "disabled")

    def save(self):
        key = self.key.get().strip()
        try:
            enabled = bool(self.proxy_enabled.get())
            proxy_url = self.proxy_url.get().strip()
            if enabled:
                proxy_url = validate_proxy_url(proxy_url)
                if not proxy_url:
                    raise NetworkError("请填写代理地址。")
            self.app.storage.update_settings({"api_key": key if self.remember.get() else "",
                                              "proxy_enabled": enabled, "proxy_url": proxy_url})
        except (OSError, NetworkError) as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.app.api_key = key
        self.app.set_status("软件设置已保存。")
        self.destroy()


class LibraryTransferDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp):
        super().__init__(app)
        self.title("资料库迁移")
        self.geometry("620x420")
        self.resizable(False, False)
        self.transient(app)
        ctk.CTkLabel(self, text="把整个资料库带到另一台电脑", font=ctk.CTkFont(size=23, weight="bold")).pack(
            anchor="w", padx=24, pady=(24, 12))
        ctk.CTkLabel(self, text=f"当前 {len(app.storage.games)} 款游戏。迁移所有游戏资料、备份、图片和已保存配置。\n"
                     "已保存的 API Key 和代理配置会随包明文保存；会话中的 Key 不进入包。",
                     text_color=MUTED, justify="left", wraplength=565).pack(anchor="w", padx=24)
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=20)
        ctk.CTkButton(actions, text="导出整个资料库", height=42, width=260,
                      command=lambda: self.begin(app.export_library)).pack(side="left")
        ctk.CTkButton(actions, text="导入整个资料库", height=42, width=260, fg_color="#303b51",
                      command=lambda: self.begin(app.import_library_archive)).pack(side="right")
        ctk.CTkLabel(self, text="导入会合并游戏和归档，保留本机已有游戏资料和路径；恢复包内配置。\n"
                     "导入后请检查游戏和存档路径，再手动还原需要使用的备份。",
                     text_color=MUTED, justify="left", wraplength=565).pack(anchor="w", padx=24)
        path = app.latest_library_export()
        ctk.CTkLabel(self, text=f"最近全库导出：\n{path}" if path else "尚未导出整个资料库。",
                     text_color=MUTED, justify="left", wraplength=565).pack(anchor="w", padx=24, pady=16)
        ctk.CTkButton(self, text="打开导出目录", width=160, fg_color="#303b51",
                      command=app.open_exports).pack(anchor="w", padx=24)
        self.after(100, self.grab_set)

    def begin(self, action):
        self.destroy()
        action()


class GameChoiceDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp, candidates: list[dict], callback, source="SteamGridDB"):
        super().__init__(app)
        self.title("选择 PCGamingWiki 匹配文章" if source == "PCGamingWiki" else
                   "选择 Steam 云存档游戏" if source == "Steam 云存档" else "选择 SteamGridDB 游戏")
        self.geometry("560x470")
        self.transient(app)
        ctk.CTkLabel(self, text="请选择准确的游戏文章" if source == "PCGamingWiki" else "请选择对应游戏",
                     font=ctk.CTkFont(size=21, weight="bold")).pack(
            anchor="w", padx=20, pady=18)
        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        for candidate in candidates:
            name = candidate["title"] if source == "PCGamingWiki" else candidate["name"]
            identity = candidate["pageid"] if source == "PCGamingWiki" else candidate["id"]
            ctk.CTkButton(frame, text=f"{name}  ·  ID {identity}", height=42,
                          anchor="w", command=lambda item=candidate: self.choose(item, callback)).pack(
                fill="x", pady=5)
        self.after(100, self.grab_set)

    def choose(self, candidate, callback):
        self.destroy()
        callback(candidate)


class SaveLocationDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp, game: dict, candidates: list[dict], callback):
        super().__init__(app)
        self.title("选择 Windows 存档目录")
        self.geometry("720x500")
        self.transient(app)
        ctk.CTkLabel(self, text=game["english_name"], font=ctk.CTkFont(size=21, weight="bold")).pack(
            anchor="w", padx=20, pady=(18, 8))
        steam_cloud = candidates[0].get("source") == "Steam 云存档"
        hint = ("以下目录由 Steam 云同步规则推导，仅适用于 Steam 版本，请确认后使用。\n"
                "含账号占位符或不明确的位置需手动填写。" if steam_cloud else
                "请选择对应版本。含占位符、注册表或不明确的位置需手动填写。")
        ctk.CTkLabel(self, text=hint, text_color=MUTED, justify="left").pack(anchor="w", padx=20, pady=(0, 12))
        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        for candidate in candidates:
            ctk.CTkLabel(frame, text=f"{candidate['label']}\n{candidate['path']}", anchor="w",
                         justify="left", wraplength=640).pack(fill="x", pady=(8, 4))
            ctk.CTkButton(frame, text="使用此目录" if candidate["resolved"] else "需手动填写",
                          state="normal" if candidate["resolved"] else "disabled",
                          command=lambda item=candidate: self.choose(item, callback)).pack(anchor="w", pady=(0, 8))
        ctk.CTkButton(self, text="打开 SteamDB 页面 ↗" if steam_cloud else "打开 PCGamingWiki 页面 ↗",
                      fg_color="transparent", border_width=1,
                      command=lambda: webbrowser.open(candidates[0]["page_url"])).pack(side="left", padx=20, pady=16)
        ctk.CTkButton(self, text="手动编辑目录", command=lambda: self.edit(app, game)).pack(
            side="right", padx=20, pady=16)
        self.after(100, self.grab_set)

    def choose(self, candidate, callback):
        self.destroy()
        callback(candidate)

    def edit(self, app, game):
        self.destroy()
        current = app.storage.get_game(game["id"])
        app.open_dialog(GameDialog, current)


class ArtworkChoiceDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp, game: dict, network: dict, result: dict, temporary: Path):
        super().__init__(app)
        self.app, self.game, self.network, self.result = app, game, network, result
        self.temporary = temporary
        self.images = []
        self.select_buttons = {}
        self.title(f"选择{ARTWORK_TITLES[result['kind']]}")
        self.geometry("880x680")
        self.minsize(760, 500)
        self.transient(app)
        ctk.CTkLabel(self, text=f"{game['english_name']} · {ARTWORK_TITLES[result['kind']]}",
                     font=ctk.CTkFont(size=21, weight="bold")).pack(anchor="w", padx=20, pady=(18, 8))
        ctk.CTkLabel(self, text=f"第 {result['page'] + 1} 页 · 选择一张图片替换当前类型，其它图片保留。",
                     text_color=MUTED).pack(anchor="w", padx=20, pady=(0, 12))
        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        for column in range(3):
            frame.grid_columnconfigure(column, weight=1, uniform="choices")
        for index, candidate in enumerate(result["candidates"]):
            card = ctk.CTkFrame(frame, fg_color=PANEL)
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=4, pady=4)
            filename = candidate.get("preview_file", "")
            preview = None
            if filename and Path(filename).name == filename and "\\" not in filename:
                preview = app.preview(temporary / "artwork" / filename, (200, 160), image_store=self.images)
            ctk.CTkLabel(card, text="" if preview else "无缩略图", image=preview, width=200, height=160,
                         text_color=MUTED).pack(padx=8, pady=(10, 4))
            ctk.CTkLabel(card, text=f"ID {candidate['id']} · {candidate.get('width', '?')} × {candidate.get('height', '?')}",
                         text_color=MUTED).pack()
            ctk.CTkLabel(card, text=candidate.get("author", {}).get("name") or "作者未提供",
                         wraplength=200).pack(padx=8, pady=(0, 6))
            button = ctk.CTkButton(card, text="使用这张", command=lambda item=candidate: self.choose(item))
            button.pack(padx=8, pady=(0, 10))
            self.select_buttons[candidate["id"]] = button
        if not result["candidates"]:
            ctk.CTkLabel(frame, text="本页没有符合规格的图片，可返回上一页或取消。").grid(
                row=0, column=0, columnspan=3, padx=16, pady=40)
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=20, pady=(0, 16))
        self.previous_button = ctk.CTkButton(controls, text="上一页", state="normal" if result["page"] else "disabled",
                                             command=lambda: self.change_page(result["page"] - 1))
        self.previous_button.pack(side="left")
        self.next_button = ctk.CTkButton(controls, text="下一页", state="normal" if result["has_next"] else "disabled",
                                         command=lambda: self.change_page(result["page"] + 1))
        self.next_button.pack(side="left", padx=10)
        ctk.CTkButton(controls, text="取消", fg_color="#303b51", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _: self.destroy())
        self.after(100, self.grab_set)

    def choose(self, candidate):
        self.destroy()
        self.app.download_single_artwork(self.game, self.network, self.result["game_id"], self.result["kind"], candidate)

    def change_page(self, page):
        self.destroy()
        self.app.load_artwork_gallery(self.game, self.network, self.result["game_id"], self.result["kind"], page)

    def destroy(self):
        super().destroy()
        shutil.rmtree(self.temporary, ignore_errors=True)


class GameManagerApp(ctk.CTk):
    def __init__(self, data_dir: Path):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title("游戏存档管理器")
        self.geometry("1220x830")
        self.minsize(1020, 720)
        self.configure(fg_color=BG)
        try:
            self.storage = Storage(data_dir)
        except (ValueError, OSError) as exc:
            self.withdraw()
            messagebox.showerror("无法打开数据目录", str(exc), parent=self)
            self.destroy()
            raise
        self.backups = BackupManager(self.storage)
        self.api_key = os.environ.get("STEAMGRIDDB_API_KEY", "") or self.storage.settings.get("api_key", "")
        self.selected_id = self.storage.games[0]["id"] if self.storage.games else None
        self.game_selection_mode = False
        self.checked_game_ids = set()
        self.busy = False
        self.artwork_job = None
        self.save_lookup_queue = []
        self.save_lookup_attempted = set()
        self._closing = False
        self.results = queue.Queue()
        self.action_buttons = []
        self.artwork_controls = []
        self.game_buttons = []
        self.game_checks = {}
        self.images = []
        self.export_paths = {}
        self.library_export_path = None
        self.dialog = None
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.sidebar = ctk.CTkFrame(self, width=245, corner_radius=0, fg_color=PANEL)
        self.sidebar.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(self.sidebar, text="存档仓库", font=ctk.CTkFont(size=22, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(28, 0), sticky="w")
        self.count_label = ctk.CTkLabel(self.sidebar, text="", text_color=MUTED)
        self.count_label.grid(row=1, column=0, padx=20, pady=(2, 16), sticky="w")
        self.search = ctk.CTkEntry(self.sidebar, placeholder_text="搜索游戏…", height=36)
        self.search.grid(row=2, column=0, padx=16, pady=(0, 10), sticky="ew")
        self.search.bind("<KeyRelease>", lambda _: self.refresh_sidebar())
        self.game_list = ctk.CTkScrollableFrame(self.sidebar, fg_color="transparent")
        self.game_list.grid(row=3, column=0, sticky="nsew", padx=8)
        sidebar_actions = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        sidebar_actions.grid(row=4, column=0, padx=16, pady=(10, 16), sticky="ew")
        sidebar_actions.grid_columnconfigure((0, 1), weight=1, uniform="sidebar_actions")
        self.add_button = ctk.CTkButton(sidebar_actions, text="＋ 添加游戏", height=34, width=100, command=self.add_game)
        self.add_button.grid(row=0, column=0, padx=(0, 3), pady=(0, 6), sticky="ew")
        self.import_button = ctk.CTkButton(sidebar_actions, text="导入游戏包", height=34, width=100,
                                          fg_color="#303b51", command=self.import_archive)
        self.import_button.grid(row=0, column=1, padx=(3, 0), pady=(0, 6), sticky="ew")
        self.library_button = ctk.CTkButton(sidebar_actions, text="资料库迁移", height=34, width=100,
                                           fg_color="#303b51", command=self.library_transfer)
        self.library_button.grid(row=1, column=0, padx=(0, 3), pady=(0, 6), sticky="ew")
        self.settings_button = ctk.CTkButton(sidebar_actions, text="软件设置", height=34, width=100,
                                            fg_color="transparent", border_width=1, command=self.settings)
        self.settings_button.grid(row=1, column=1, padx=(3, 0), pady=(0, 6), sticky="ew")
        delete_controls = ctk.CTkFrame(sidebar_actions, fg_color="transparent")
        delete_controls.grid(row=2, column=0, columnspan=2, sticky="ew")
        delete_controls.grid_columnconfigure(0, weight=1)
        self.delete_games_button = ctk.CTkButton(delete_controls, text="删除勾选游戏", height=34, width=140,
                                                fg_color="#703d4a", hover_color="#8e4b5d",
                                                command=self.delete_checked_games)
        self.delete_games_button.grid(row=0, column=0, sticky="ew")
        self.cancel_game_selection_button = ctk.CTkButton(delete_controls, text="取消", width=52, height=34,
                                                          fg_color="#303b51", command=self.cancel_game_selection)
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, padx=24, pady=(22, 8), sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(3, weight=1)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=1, padx=24, pady=(0, 15), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(footer, text="就绪", text_color=MUTED, anchor="w", wraplength=650)
        self.status.grid(row=0, column=0, sticky="w")
        self.progress = ctk.CTkProgressBar(footer, width=100, mode="indeterminate")
        self.stop_artwork_button = ctk.CTkButton(footer, text="停止获取", width=100,
                                               fg_color="#703d4a", command=self.stop_artwork)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.after(100, self.poll_results)
        self.after(150, lambda: self.lookup_save_path(self.selected_game()))

    def set_status(self, text):
        self.status.configure(text=text)

    def refresh_status(self):
        if self.busy:
            return
        job = self.artwork_job
        if job is None:
            self.set_status("就绪")
        elif job["cancelled"]:
            self.set_status("正在停止存档目录查询…" if job["action"] == "save_lookup" else "正在停止图片获取…")
        else:
            self.set_status(job["status"])

    def selected_game(self):
        try:
            return self.storage.get_game(self.selected_id) if self.selected_id else None
        except ValueError:
            return None

    def button(self, parent, text, command, **kwargs):
        kwargs.setdefault("height", 36)
        button = ctk.CTkButton(parent, text=text, command=command, **kwargs)
        button.configure(state="disabled" if self.busy else "normal")
        self.action_buttons.append(button)
        return button

    def refresh_sidebar(self):
        for widget in self.game_list.winfo_children():
            widget.destroy()
        self.count_label.configure(text=f"{len(self.storage.games)} 款游戏 · 本地存档仓库")
        self.game_buttons = []
        self.game_checks = {}
        self.checked_game_ids.intersection_update(game["id"] for game in self.storage.games)
        if not self.storage.games:
            self.game_selection_mode = False
        query = self.search.get().strip().casefold()
        for game in self.storage.games:
            if query and query not in f"{game['english_name']} {game.get('chinese_name', '')}".casefold():
                continue
            text = game.get("chinese_name") or game["english_name"]
            if game.get("chinese_name"):
                text += f"\n{game['english_name']}"
            row = ctk.CTkFrame(self.game_list, fg_color="transparent")
            row.pack(fill="x", pady=4)
            column = 1 if self.game_selection_mode else 0
            row.grid_columnconfigure(column, weight=1)
            if self.game_selection_mode:
                check = ctk.CTkCheckBox(row, text="", width=22, checkbox_width=18, checkbox_height=18,
                                       command=lambda gid=game["id"]: self.toggle_game_check(gid))
                check.grid(row=0, column=0, padx=(0, 6))
                if game["id"] in self.checked_game_ids:
                    check.select()
                check.configure(state="disabled" if self.busy or self._closing else "normal")
                self.game_checks[game["id"]] = check
            button = ctk.CTkButton(row, text=text, anchor="w", height=60,
                                   fg_color=ACCENT if game["id"] == self.selected_id else "transparent",
                                   hover_color="#2c3750", state="disabled" if self.busy else "normal",
                                   command=lambda gid=game["id"]: self.select(gid))
            button.grid(row=0, column=column, sticky="ew")
            self.game_buttons.append(button)
        self.update_delete_games_button()

    def toggle_game_check(self, game_id):
        if self.busy or self._closing or not self.game_selection_mode:
            return
        if self.game_checks[game_id].get():
            self.checked_game_ids.add(game_id)
        else:
            self.checked_game_ids.discard(game_id)
        self.update_delete_games_button()

    def update_delete_games_button(self):
        text = f"删除勾选（{len(self.checked_game_ids)}）" if self.game_selection_mode else "删除勾选游戏"
        disabled = self.busy or self._closing or not self.storage.games or (self.game_selection_mode and not self.checked_game_ids)
        self.delete_games_button.configure(text=text, state="disabled" if disabled else "normal")
        self.cancel_game_selection_button.configure(state="disabled" if self.busy or self._closing else "normal")
        if self.game_selection_mode:
            self.cancel_game_selection_button.grid(row=0, column=1, padx=(6, 0))
        else:
            self.cancel_game_selection_button.grid_remove()

    def cancel_game_selection(self):
        if self.busy or self._closing:
            return
        self.game_selection_mode = False
        self.checked_game_ids.clear()
        self.refresh_sidebar()

    def select(self, game_id):
        if self.busy:
            return
        self.selected_id = game_id
        self.refresh()
        self.refresh_status()
        self.lookup_save_path(self.selected_game())

    def refresh(self):
        current_tab = self.tabs.get() if hasattr(self, "tabs") and self.tabs.winfo_exists() else "存档备份"
        self.refresh_sidebar()
        for widget in self.content.winfo_children():
            widget.destroy()
        self.action_buttons = []
        self.artwork_controls = []
        self.images = []
        game = self.selected_game()
        if not game:
            ctk.CTkLabel(self.content, text="从添加第一款游戏开始", font=ctk.CTkFont(size=29, weight="bold")).grid(
                row=0, column=0, pady=(150, 16))
            ctk.CTkLabel(self.content, text="管理存档快照、下载游戏图片、导出冷盘备份包。", text_color=MUTED).grid(
                row=1, column=0)
            return
        header = ctk.CTkFrame(self.content, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=game.get("chinese_name") or game["english_name"],
                     font=ctk.CTkFont(size=28, weight="bold"), anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text=game["english_name"], text_color=MUTED, anchor="w").grid(row=1, column=0, sticky="w")
        self.button(header, "编辑资料", lambda: self.open_dialog(GameDialog, game), width=92,
                    fg_color="#303b51").grid(row=0, column=1, padx=6)
        self.button(header, "删除当前", self.delete_game, width=92,
                    fg_color="#703d4a", hover_color="#8e4b5d").grid(row=0, column=2, padx=6)
        self.render_banner(game)
        paths = ctk.CTkFrame(self.content, fg_color="transparent")
        paths.grid(row=2, column=0, sticky="ew", pady=(10, 14))
        paths.grid_columnconfigure(0, weight=1)
        for row, (label, key) in enumerate((("游戏目录", "game_path"), ("存档目录", "save_path"))):
            ctk.CTkLabel(paths, text=f"{label}  ·  {game.get(key) or '尚未填写'}", text_color=MUTED,
                         anchor="w", wraplength=650).grid(row=row, column=0, sticky="w", pady=3)
            self.button(paths, "打开", lambda k=key: self.open_path(game.get(k, "")), width=65,
                        fg_color="#28334a").grid(row=row, column=1, padx=(12, 0), pady=3)
            if key == "save_path" and not game.get(key, "").strip():
                self.button(paths, "查询目录", lambda: self.lookup_save_path(game, force=True), width=85,
                            fg_color="#28334a").grid(row=row, column=2, padx=(6, 0), pady=3)
        tabs = ctk.CTkTabview(self.content, fg_color=PANEL, command=self.refresh_status)
        self.tabs = tabs
        tabs.grid(row=3, column=0, sticky="nsew")
        self.render_backups(tabs.add("存档备份"), game)
        self.render_artwork(tabs.add("游戏图片"), game)
        self.render_export(tabs.add("数据打包"), game)
        tabs.set(current_tab)

    def artwork_manifest(self, game):
        path = self.storage.artwork_dir(game) / "assets.json"
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("assets"), dict):
                return {"assets": {}, "warning": "图片来源记录损坏，请重新获取图片。"}
            for asset in value["assets"].values():
                if not isinstance(asset, dict) or not isinstance(asset.get("file"), str):
                    return {"assets": {}, "warning": "图片来源记录损坏，请重新获取图片。"}
                filename = Path(asset["file"])
                if filename.name != asset["file"] or "\\" in asset["file"]:
                    return {"assets": {}, "warning": "图片来源记录损坏，请重新获取图片。"}
            if not isinstance(value.get("missing", []), list) or any(
                    not isinstance(item, str) for item in value.get("missing", [])):
                return {"assets": {}, "warning": "图片来源记录损坏，请重新获取图片。"}
            return value
        except (OSError, ValueError):
            return {"assets": {}, "warning": "图片来源记录损坏，请重新获取图片。"}

    def preview(self, path, size, crop=False, image_store=None):
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
            if crop:
                image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            else:
                image.thumbnail(size, Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", size, PANEL)
                canvas.alpha_composite(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
                image = canvas
            result = ctk.CTkImage(light_image=image, dark_image=image, size=size)
            (self.images if image_store is None else image_store).append(result)
            return result
        except (OSError, ValueError):
            return None

    def render_banner(self, game):
        asset = self.artwork_manifest(game).get("assets", {}).get("hero", {})
        path = self.storage.artwork_dir(game) / asset.get("file", "hero.png")
        image = None
        try:
            with Image.open(path) as source:
                original = source.convert("RGBA")
            scale = min(600 / original.width, 150 / original.height)
            image = ctk.CTkImage(light_image=original, dark_image=original,
                                 size=(max(1, int(original.width * scale)), max(1, int(original.height * scale))))
            self.images.append(image)
        except (OSError, ValueError):
            pass
        banner = ctk.CTkLabel(self.content, text="" if image else "每一次冒险，都有一份归档。",
                             image=image, width=1, height=150, fg_color=PANEL, corner_radius=12,
                             font=ctk.CTkFont(size=21), text_color="#b9c9ef")
        banner.grid_propagate(False)
        banner.grid(row=1, column=0, sticky="ew")
        if image is not None:
            def resize_banner(_):
                width = max(1, banner.winfo_width() / banner._get_widget_scaling() - 24)
                ratio = min(width / original.width, 150 / original.height)
                size = (max(1, int(original.width * ratio)), max(1, int(original.height * ratio)))
                if image.cget("size") != size:
                    image.configure(size=size)
            Frame.bind(banner, "<Configure>", resize_banner, add="+")

    def render_backups(self, parent, game):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        self.button(bar, "＋ 立即备份", self.create_backup, width=130).pack(side="left")
        self.button(bar, "打开备份目录", lambda: self.open_game_data(game, "backups"),
                    width=130, fg_color="#303b51").pack(side="left", padx=10)
        ctk.CTkLabel(bar, text="还原将直接替换存档目录。", text_color=MUTED).pack(side="right")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Saves.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground="#e3e9f5", rowheight=38, borderwidth=0)
        style.configure("Saves.Treeview.Heading", background="#28334a", foreground="#c1cfe3", relief="flat")
        style.map("Saves.Treeview", background=[("selected", "#3a5390")], foreground=[("selected", "white")])
        table_frame = ctk.CTkFrame(parent, fg_color="transparent")
        table_frame.grid(row=1, column=0, sticky="nsew", padx=12)
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)
        self.backup_table = ttk.Treeview(table_frame, columns=("sequence", "time", "size"),
                                         show="headings", selectmode="extended", style="Saves.Treeview")
        for key, text, width in (("sequence", "编号", 65), ("time", "本地备份时间", 210),
                                 ("size", "压缩大小", 95)):
            self.backup_table.heading(key, text=text, anchor="center")
            self.backup_table.column(key, width=width, minwidth=60, stretch=key == "time", anchor="center")
        self.backup_table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.backup_table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.backup_table.configure(yscrollcommand=scrollbar.set)
        try:
            records = self.backups.list_backups(game)
        except (ValueError, OSError) as exc:
            records = []
            ctk.CTkLabel(parent, text=f"{exc}\n请打开备份目录检查，原文件已保留。",
                         text_color="#ff9d9d", wraplength=740, justify="left").grid(
                row=3, column=0, sticky="w", padx=12, pady=(0, 8))
        for backup in records:
            time = backup["created_at"].replace("T", " ")
            self.backup_table.insert("", "end", iid=backup["id"], values=(
                f"#{backup['sequence']:06d}", time, readable_size(backup["size"])))
        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=2, column=0, padx=12, pady=14, sticky="ew")
        for text, callback in (("还原所选", self.restore_backup), ("复制备份", self.copy_backup),
                               ("打开所选目录", self.open_backup), ("删除所选", self.delete_backup)):
            self.button(actions, text, callback, width=118, fg_color="#303b51").pack(side="left", padx=(0, 8))
        ctk.CTkLabel(parent, text="按住 Ctrl（macOS 为 ⌘）点选多条，或用 Shift 连选，再点击删除所选。",
                     text_color=MUTED, anchor="w").grid(row=4, column=0, padx=12, pady=(0, 8), sticky="w")

    def render_artwork(self, parent, game):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        self.artwork_button = self.button(bar, "搜索并获取图片", self.search_artwork, width=145)
        self.artwork_button.pack(side="left")
        if self.artwork_job is not None:
            self.artwork_button.configure(state="disabled")
        self.button(bar, "打开图片目录", lambda: self.open_game_data(game, "artwork"),
                    width=125, fg_color="#303b51").pack(side="left", padx=10)
        self.button(bar, "查看网站 ↗", lambda: webbrowser.open(
            f"https://www.steamgriddb.com/game/{game['steamgrid_id']}" if game.get("steamgrid_id")
            else "https://www.steamgriddb.com"), width=110, fg_color="#303b51").pack(side="left")
        ctk.CTkLabel(parent, text="可更改本地图片，或按指定规格再搜索并选择一张图片。搜索同名游戏时可选择匹配结果。",
                     text_color=MUTED, wraplength=750).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))
        body = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        body.grid_columnconfigure(0, weight=1)
        cards = ctk.CTkFrame(body, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="ew")
        manifest = self.artwork_manifest(game)
        assets = manifest.get("assets", {})
        for column, (key, title, dimensions) in enumerate((
                ("cover", "竖版封面", "600 × 900"), ("wide", "横版大图", "920 × 430"),
                ("hero", "标题背景", "1920 × 620"), ("logo", "游戏 Logo", "原始尺寸"))):
            cards.grid_columnconfigure(column, weight=1, uniform="cards")
            card = ctk.CTkFrame(cards, fg_color="#222c3e")
            card.grid(row=0, column=column, padx=4, sticky="nsew")
            asset = assets.get(key, {})
            path = self.storage.artwork_dir(game) / asset.get("file", "missing")
            image = self.preview(path, (150, 145)) if path.is_file() else None
            ctk.CTkLabel(card, text="" if image else "待获取", image=image, width=150, height=145,
                         text_color=MUTED).pack(padx=8, pady=(12, 6))
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(weight="bold")).pack()
            ctk.CTkLabel(card, text=dimensions, text_color=MUTED).pack(pady=(0, 8))
            controls = ctk.CTkFrame(card, fg_color="transparent")
            controls.pack(fill="x", padx=6, pady=(0, 10))
            for text, callback in (("更改", lambda k=key: self.change_artwork(game, k)),
                                   ("再搜索", lambda k=key: self.search_artwork_kind(game, k))):
                button = self.button(controls, text, callback, width=66, height=30, fg_color="#303b51")
                button.pack(side="left", expand=True, fill="x", padx=2)
                self.artwork_controls.append(button)
                if self.artwork_job is not None:
                    button.configure(state="disabled")
        if manifest.get("missing"):
            ctk.CTkLabel(body, text="该游戏缺少图片：" + "、".join(manifest["missing"]),
                         text_color="#e4bd7e").grid(row=1, column=0, padx=12, pady=12, sticky="w")
        if manifest.get("warning"):
            ctk.CTkLabel(body, text=manifest["warning"], text_color="#ff9d9d").grid(
                row=2, column=0, padx=12, pady=12, sticky="w")

    def render_export(self, parent, game):
        parent.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(parent, text="把这款游戏的归档带走", font=ctk.CTkFont(size=23, weight="bold")).grid(
            row=0, column=0, padx=24, pady=(28, 12), sticky="w")
        ctk.CTkLabel(parent, text="导出单个 ZIP：包含此游戏全部现有备份、已下载图片、图片来源及游戏资料。\n"
                     "游戏本体和 API Key 不进入包内。可直接复制到冷盘保存。", text_color=MUTED,
                     justify="left", wraplength=740).grid(row=1, column=0, padx=24, pady=(0, 20), sticky="w")
        self.button(parent, "打包此游戏全部备份与图片", self.export_game, width=240, height=42).grid(
            row=2, column=0, padx=24, pady=10, sticky="w")
        path = self.export_paths.get(game["id"])
        if path is None:
            exports = self.storage.data_dir / "exports"
            matches = sorted(exports.glob(f"*{game['id']}*.zip"), key=lambda p: p.stat().st_mtime) if exports.exists() else []
            path = matches[-1] if matches else None
        ctk.CTkLabel(parent, text=f"最近导出：\n{path}" if path else "尚未导出。",
                     text_color=MUTED, justify="left", wraplength=740).grid(row=3, column=0, padx=24, pady=12, sticky="w")
        self.button(parent, "打开导出文件夹", lambda: self.open_exports(), width=160,
                    fg_color="#303b51").grid(row=4, column=0, padx=24, sticky="w")
        self.button(parent, "从 ZIP 导入游戏数据包", self.import_archive, width=200,
                    fg_color="#303b51").grid(row=5, column=0, padx=24, pady=(16, 0), sticky="w")

    def add_game(self):
        if not self.busy:
            self.open_dialog(GameDialog)

    def settings(self):
        if not self.busy:
            self.open_dialog(SettingsDialog)

    def library_transfer(self):
        if not self.busy and not self._closing:
            self.open_dialog(LibraryTransferDialog)

    def open_dialog(self, dialog_class, *args):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            self.dialog.focus_force()
        else:
            self.dialog = dialog_class(self, *args)

    def delete_game(self):
        if self.busy or self._closing:
            return
        game = self.selected_game()
        if game:
            self.delete_games([game])

    def delete_checked_games(self):
        if self.busy or self._closing or not self.storage.games:
            return
        if not self.game_selection_mode:
            self.game_selection_mode = True
            self.checked_game_ids.clear()
            self.refresh_sidebar()
            return
        games = [dict(game) for game in self.storage.games if game["id"] in self.checked_game_ids]
        self.delete_games(games)

    def delete_games(self, games):
        if self.busy or self._closing or not games:
            return
        names = "\n".join(f"• {game['english_name']}" for game in games)
        prompt = (f"删除 {games[0]['english_name']} 及其全部备份和图片？" if len(games) == 1
                  else f"删除勾选的 {len(games)} 款游戏及其全部备份和图片？\n{names}")
        if not messagebox.askyesno("删除游戏", prompt + "\n软件内的归档将永久删除，游戏本体和实际存档保留。", parent=self):
            return
        ids = {game["id"] for game in games}
        if self.artwork_job is not None and self.artwork_job["game"]["id"] in ids:
            self.artwork_job["cancelled_for_deletion"] = True
            self.stop_artwork()
        self.save_lookup_queue = [item for item in self.save_lookup_queue if item["game"]["id"] not in ids]

        def work():
            deleted = []
            for game in games:
                try:
                    self.storage.get_game(game["id"])
                    self.backups._root(game)
                    directory = self.storage.game_dir(game)
                    if directory.exists() and not directory.is_dir():
                        raise ValueError("游戏数据位置必须是普通文件夹。")
                except (OSError, ValueError) as exc:
                    return {"deleted": [], "warning": f"删除前校验失败：{game['english_name']}：{exc}"}
            for game in games:
                try:
                    warning = self.delete_game_data(game)
                except Exception as exc:
                    warning = str(exc)
                if warning:
                    return {"deleted": deleted, "warning": f"失败游戏：{game['english_name']}\n{warning}"}
                deleted.append(game["id"])
            return {"deleted": deleted, "warning": None}

        def complete(result):
            if self.selected_id not in {item["id"] for item in self.storage.games}:
                self.selected_id = self.storage.games[0]["id"] if self.storage.games else None
            self.checked_game_ids.intersection_update(item["id"] for item in self.storage.games)
            if not result["warning"] and not self.checked_game_ids:
                self.game_selection_mode = False
            self.refresh()
            if result["warning"]:
                self.show_error(RuntimeError(f"已删除 {len(result['deleted'])} / 共 {len(games)} 款游戏。\n{result['warning']}"))
            else:
                target = games[0]["english_name"] if len(games) == 1 else f"{len(games)} 款游戏"
                self.set_status(f"已删除 {target} 及其全部备份和图片。")
        self.run_task("正在删除游戏及全部备份和图片…", work, complete)

    def delete_game_data(self, game):
        self.backups._root(game)
        directory = self.storage.game_dir(game)
        if directory.exists() and not directory.is_dir():
            raise ValueError("游戏数据位置必须是普通文件夹。")
        library = {"games": [dict(item) for item in self.storage.games], "settings": dict(self.storage.settings)}
        temporary = directory.with_name(f".deleting-{uuid.uuid4().hex}") if directory.exists() else None
        if temporary is not None:
            directory.rename(temporary)
        try:
            self.storage.delete_game(game["id"])
        except (OSError, ValueError):
            if temporary is not None:
                try:
                    temporary.rename(directory)
                except OSError as exc:
                    raise RuntimeError(f"游戏记录删除失败，归档目录恢复未完成；资源保留在 {temporary}，请手动恢复。") from exc
            raise
        if temporary is not None:
            try:
                shutil.rmtree(temporary)
            except OSError as exc:
                location = temporary
                details = []
                try:
                    if temporary.exists():
                        temporary.rename(directory)
                        location = directory
                except OSError as rollback:
                    details.append(f"目录恢复失败：{rollback}")
                try:
                    self.storage.replace_library(library)
                except (OSError, ValueError) as rollback:
                    details.append(f"游戏条目恢复失败：{rollback}")
                return (f"游戏删除未完成：{exc}。部分文件可能已删除，请检查归档。\n"
                        f"剩余资源位置：{location}" + ("\n" + "；".join(details) if details else "\n游戏条目已保留。"))
        return None

    def show_error(self, error):
        self.set_status("操作失败，请查看提示。")
        messagebox.showerror("操作失败", str(error), parent=self)

    def open_path(self, value):
        try:
            if not value:
                raise ValueError("请先编辑游戏资料并填写目录。")
            open_folder(local_path(value))
        except (OSError, ValueError) as exc:
            self.show_error(exc)

    def open_game_data(self, game, name):
        path = self.storage.game_dir(game) / name
        try:
            path.mkdir(parents=True, exist_ok=True)
            open_folder(path)
        except OSError as exc:
            self.show_error(exc)

    def open_exports(self):
        try:
            path = self.storage.data_dir / "exports"
            path.mkdir(parents=True, exist_ok=True)
            open_folder(path)
        except OSError as exc:
            self.show_error(exc)

    def backup_selection(self):
        selection = self.backup_table.selection()
        if not selection:
            messagebox.showinfo("选择备份", "请先在列表中选择一条备份。", parent=self)
            return None
        if len(selection) != 1:
            messagebox.showinfo("选择备份", "此操作需要只选择一条备份；多选可用于删除。", parent=self)
            return None
        return selection[0]

    def create_backup(self):
        game = dict(self.selected_game())
        if not game.get("save_path", "").strip():
            self.lookup_save_path(game, self.backup_game, force=True)
            return
        self.backup_game(game)

    def backup_game(self, game):
        self.run_task("正在创建存档快照…", lambda: self.backups.create(game),
                      lambda result: self.operation_done(f"备份完成：#{result['sequence']:06d}"))

    def copy_backup(self):
        backup_id = self.backup_selection()
        if backup_id:
            game = dict(self.selected_game())
            self.run_task("正在复制备份…", lambda: self.backups.copy(game, backup_id),
                          lambda result: self.operation_done(f"复制完成：#{result['sequence']:06d}"))

    def restore_backup(self):
        backup_id = self.backup_selection()
        if not backup_id:
            return
        game = dict(self.selected_game())
        if not game.get("save_path", "").strip():
            self.lookup_save_path(game, lambda saved: self.confirm_restore(saved, backup_id), force=True)
            return
        self.confirm_restore(game, backup_id)

    def confirm_restore(self, game, backup_id):
        if messagebox.askyesno("还原存档", "请先关闭游戏。\n还原将直接替换以下目录：\n"
                              + game["english_name"] + "\n" + game["save_path"] + "\n是否继续？", parent=self):
            self.run_task("正在校验并还原存档…", lambda: self.backups.restore(game, backup_id),
                          lambda warning: self.operation_done("还原完成。" + (warning or "")))

    def delete_backup(self):
        backup_ids = list(self.backup_table.selection())
        if not backup_ids:
            messagebox.showinfo("选择备份", "请先选择要删除的一条或多条备份。", parent=self)
            return
        if messagebox.askyesno("删除备份", f"永久删除所选的 {len(backup_ids)} 条备份？当前游戏存档不受影响。", parent=self):
            game = dict(self.selected_game())
            self.run_task("正在删除所选备份…", lambda: self.backups.delete_many(game, backup_ids),
                          lambda count: self.operation_done(f"已删除 {count} 条备份。"))

    def open_backup(self):
        backup_id = self.backup_selection()
        if backup_id:
            try:
                open_folder(self.backups.backup_dir(self.selected_game(), backup_id))
            except (ValueError, OSError) as exc:
                self.show_error(exc)

    def lookup_save_path(self, game, on_resolved=None, force=False):
        if not game or game.get("save_path", "").strip() or self._closing:
            return
        key = (game["id"], game["english_name"])
        job = self.artwork_job
        if (job is not None and not job["cancelled"] and job["action"] == "save_lookup"
                and (job["game"]["id"], job["game"]["english_name"]) == key):
            if on_resolved is not None:
                job["on_resolved"] = on_resolved
            return
        for pending in self.save_lookup_queue:
            if (pending["game"]["id"], pending["game"]["english_name"]) == key:
                if on_resolved is not None:
                    pending["on_resolved"] = on_resolved
                return
        if not force and key in self.save_lookup_attempted:
            return
        self.save_lookup_attempted.add(key)
        self.save_lookup_queue.append({"game": dict(game), "on_resolved": on_resolved})
        self.start_pending_lookup()

    def current_lookup_game(self, game):
        try:
            current = self.storage.get_game(game["id"])
        except ValueError:
            self.set_status("游戏已删除，已丢弃存档目录查询结果。")
            return None
        if current["english_name"] != game["english_name"] or current.get("save_path", "").strip():
            self.set_status("游戏名称或存档目录已修改，已丢弃旧查询结果。")
            return None
        return current

    def start_pending_lookup(self):
        if (self.busy or self.artwork_job is not None or self._closing
                or (self.dialog is not None and self.dialog.winfo_exists())):
            return
        while self.save_lookup_queue:
            pending = self.save_lookup_queue.pop(0)
            game = self.current_lookup_game(pending["game"])
            if game is None:
                continue
            try:
                network = pending["network"] if "network" in pending else {"proxy_url": settings_proxy_url(self.storage.settings)}
            except NetworkError as exc:
                self.show_error(exc)
                return
            self.start_artwork_job("save_lookup", game, network, pending.get("value", game["english_name"]))
            if self.artwork_job is not None:
                self.artwork_job["on_resolved"] = pending["on_resolved"]
            return

    def save_location_results(self, game, candidates, on_resolved, network=None):
        current = self.current_lookup_game(game)
        if current is None:
            return
        source = candidates.get("source", "PCGamingWiki") if isinstance(candidates, dict) else "PCGamingWiki"
        if isinstance(candidates, dict) and "games" in candidates:
            self.set_status(f"请为 {game['english_name']} 选择准确的 {source} 游戏。")
            self.open_dialog(GameChoiceDialog, candidates["games"],
                             lambda candidate: self.choose_save_game(game, network, candidate, on_resolved),
                             source)
            return
        if isinstance(candidates, dict):
            candidates = candidates.get("locations", [])
        if candidates:
            source = candidates[0].get("source", "PCGamingWiki")
        if not candidates:
            sources = "PCGamingWiki 和 Steam 云存档" if source == "Steam 云存档" else source
            self.set_status(f"{sources} 未提供 {game['english_name']} 的 Windows 存档目录，请手动填写或点击查询目录重试。")
        elif len(candidates) == 1 and candidates[0]["resolved"] and source != "Steam 云存档":
            self.use_save_location(current, candidates[0], on_resolved)
        else:
            if source == "Steam 云存档":
                self.set_status(f"已从 Steam 云存档取得 {game['english_name']} 的目录规则，请确认目录后使用。")
            self.open_dialog(SaveLocationDialog, current, candidates,
                             lambda candidate: self.use_save_location(game, candidate, on_resolved))

    def choose_save_game(self, game, network, candidate, on_resolved):
        current = self.current_lookup_game(game)
        if current is None or self._closing:
            return
        for pending in self.save_lookup_queue:
            if (pending["game"]["id"], pending["game"]["english_name"]) == (current["id"], current["english_name"]):
                self.save_lookup_queue.remove(pending)
                if pending["on_resolved"] is not None:
                    on_resolved = pending["on_resolved"]
                break
        self.save_lookup_queue.insert(0, {"game": dict(current), "network": network, "value": candidate,
                                         "on_resolved": on_resolved})
        self.start_pending_lookup()

    def use_save_location(self, game, candidate, on_resolved):
        current = self.current_lookup_game(game)
        if current is None or not candidate["resolved"]:
            return
        try:
            saved = self.storage.save_game({**current, "save_path": candidate["path"]})
        except (ValueError, OSError) as exc:
            self.show_error(exc)
            return
        self.refresh()
        source = candidate.get("source", "PCGamingWiki")
        self.set_status(f"已从 {source} 填入 {saved['english_name']} 的 Windows 存档目录：{saved['save_path']}")
        if on_resolved is not None:
            on_resolved(saved)

    def change_artwork(self, game, kind):
        if self.busy or self.artwork_job is not None or self._closing:
            return
        path = filedialog.askopenfilename(parent=self, title=f"更改{ARTWORK_TITLES[kind]}",
                                          filetypes=[("静态图片", "*.png *.jpg *.jpeg *.webp"), ("所有文件", "*.*")])
        if not path or self.busy or self.artwork_job is not None or self._closing:
            return
        current = self.current_artwork_game(game)
        if current is None:
            return
        def work():
            self.backups._root(current)
            return replace_local_asset(kind, Path(path), self.storage.artwork_dir(current))
        self.run_task(f"正在更改{ARTWORK_TITLES[kind]}…", work,
                      lambda _: self.operation_done(f"{current['english_name']} 的{ARTWORK_TITLES[kind]}已更改。"))

    def search_artwork_kind(self, game, kind):
        if self.busy or self.artwork_job is not None or self._closing:
            return
        if not self.api_key:
            self.open_dialog(SettingsDialog)
            self.set_status("填写 API Key 后，再点击再搜索。")
            return
        current = self.current_artwork_game(game)
        if current is None:
            return
        try:
            network = {"api_key": self.api_key, "proxy_url": settings_proxy_url(self.storage.settings)}
        except NetworkError as exc:
            self.show_error(exc)
            return
        if current.get("steamgrid_id"):
            self.load_artwork_gallery(current, network, current["steamgrid_id"], kind)
        else:
            job = self.start_artwork_job("search", current, network, current["english_name"])
            if job is not None:
                job["kind"] = kind

    def kind_game_candidates(self, game, network, candidates, kind):
        if not candidates:
            messagebox.showinfo("没有匹配结果", "没有找到对应游戏，请检查英文名。", parent=self)
            self.set_status("SteamGridDB 未找到游戏。")
        elif len(candidates) == 1:
            self.load_artwork_gallery(game, network, candidates[0]["id"], kind)
        else:
            self.open_dialog(GameChoiceDialog, candidates,
                             lambda candidate: self.load_artwork_gallery(game, network, candidate["id"], kind))

    def load_artwork_gallery(self, game, network, game_id, kind, page=0):
        current = self.current_artwork_game(game)
        if current is not None:
            self.start_artwork_job("gallery", current, network, {"game_id": game_id, "kind": kind, "page": page})

    def download_single_artwork(self, game, network, game_id, kind, candidate):
        current = self.current_artwork_game(game)
        if current is not None:
            self.start_artwork_job("download_one", current, network, {"game_id": game_id, "kind": kind, "candidate": candidate})

    def search_artwork(self):
        if self.busy or self.artwork_job is not None:
            return
        if not self.api_key:
            self.open_dialog(SettingsDialog)
            self.set_status("填写 API Key 后，再点击搜索并获取图片。")
            return
        game = dict(self.selected_game())
        try:
            proxy_url = settings_proxy_url(self.storage.settings)
        except NetworkError as exc:
            self.show_error(exc)
            return
        network = {"api_key": self.api_key, "proxy_url": proxy_url}
        self.start_artwork_job("search", game, network, game["english_name"])

    def artwork_candidates(self, game, network, candidates):
        if not candidates:
            messagebox.showinfo("没有匹配结果", "没有找到对应游戏，请检查英文名。", parent=self)
            self.set_status("SteamGridDB 未找到游戏。")
        elif len(candidates) == 1:
            self.download_artwork(game, network, candidates[0])
        else:
            self.open_dialog(GameChoiceDialog, candidates,
                             lambda candidate: self.download_artwork(game, network, candidate))

    def download_artwork(self, game, network, candidate):
        if self.current_artwork_game(game) is not None:
            self.start_artwork_job("download", game, network, candidate["id"])

    def current_artwork_game(self, game):
        try:
            current = self.storage.get_game(game["id"])
        except ValueError:
            self.set_status("游戏已删除，已丢弃图片获取结果。")
            return None
        if current["english_name"] != game["english_name"]:
            self.set_status("游戏英文名已更改，请重新搜索图片。")
            return None
        return current

    def start_artwork_job(self, action, game, network, value):
        if self.busy or self.artwork_job is not None or self._closing:
            return
        context = multiprocessing.get_context("spawn")
        temporary = receiver = sender = process = None
        try:
            temporary = Path(tempfile.mkdtemp(prefix=".artwork-", dir=self.storage.data_dir))
            receiver, sender = context.Pipe(duplex=False)
            if action == "save_lookup":
                process = context.Process(target=pcgw_worker, args=(sender, network["proxy_url"], value), daemon=True)
            else:
                process = context.Process(target=artwork_worker,
                                          args=(sender, network["api_key"], network["proxy_url"], action,
                                                value, temporary / "artwork"), daemon=True)
            process.start()
        except Exception:
            if receiver is not None:
                receiver.close()
            if process is not None:
                process.close()
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            self.show_error(NetworkError("无法启动联网后台进程，请重试。"))
            return
        finally:
            if sender is not None:
                sender.close()
        self.artwork_job = {"action": action, "game": dict(game), "network": network, "value": value,
                            "temporary": temporary, "process": process, "receiver": receiver,
                            "cancelled": False}
        self.artwork_job["status"] = (f"正在查询 {game['english_name']} 的存档目录…" if action == "save_lookup"
                                      else f"正在{'搜索' if action == 'search' else '获取'} {game['english_name']} 的图片…")
        if action in ("gallery", "download_one"):
            self.artwork_job["status"] = f"正在{'搜索' if action == 'gallery' else '下载'} {game['english_name']} 的{ARTWORK_TITLES[value['kind']]}…"
        self.refresh_status()
        self.set_busy_widgets()
        self.update_activity()
        return self.artwork_job

    def stop_artwork(self):
        self.save_lookup_queue.clear()
        job = self.artwork_job
        if job is None or job["cancelled"]:
            return
        job["cancelled"] = True
        job["stopped_at"] = time.monotonic()
        if job["process"].is_alive():
            job["process"].terminate()
        self.stop_artwork_button.configure(state="disabled")
        if not self.busy:
            self.set_status("正在停止存档目录查询…" if job["action"] == "save_lookup" else "正在停止图片获取…")

    def publish_artwork(self, job, game):
        self.backups._root(game)
        destination = self.storage.artwork_dir(game)
        if (destination.is_symlink() or destination.is_junction()
                or (destination.exists() and not destination.is_dir())):
            raise ArtworkError("图片存储位置必须是普通文件夹。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = job["temporary"] / "artwork"
        if job["action"] == "download_one":
            merge_assets(staging, destination)
        previous = job["temporary"] / "previous"
        moved = published = False
        try:
            if destination.exists():
                destination.rename(previous)
                moved = True
            staging.rename(destination)
            published = True
            game_id = job["value"]["game_id"] if job["action"] == "download_one" else job["value"]
            self.storage.save_game({**game, "steamgrid_id": game_id})
        except Exception:
            try:
                if published:
                    destination.rename(staging)
                if moved:
                    previous.rename(destination)
            except OSError as exc:
                job["preserve"] = True
                raise RuntimeError(f"图片更新失败且回滚未完成，保留资源位于 {job['temporary']}，请手动恢复。") from exc
            raise

    def poll_artwork(self):
        job = self.artwork_job
        if job is None:
            return
        process, receiver = job["process"], job["receiver"]
        if job["cancelled"]:
            if process.is_alive() and time.monotonic() - job["stopped_at"] > 2:
                process.kill()
        elif "result" not in job and receiver.poll():
            try:
                job["result"] = receiver.recv()
            except (EOFError, OSError):
                job["result"] = (False, "联网后台进程异常退出，请重试。")
        dialog_open = self.dialog is not None and self.dialog.winfo_exists()
        if process.is_alive() or (not job["cancelled"] and (self.busy or dialog_open)):
            return
        process.join(timeout=0)
        if not job["cancelled"] and "result" not in job:
            try:
                job["result"] = receiver.recv() if receiver.poll() else (False, "联网后台进程异常退出，请重试。")
            except (EOFError, OSError):
                job["result"] = (False, "联网后台进程异常退出，请重试。")
        receiver.close()
        process.close()
        self.artwork_job = None
        self.set_busy_widgets()
        self.update_activity()
        try:
            if job["cancelled"]:
                if not self.busy and not job.get("cancelled_for_deletion"):
                    self.set_status("存档目录查询已停止。" if job["action"] == "save_lookup"
                                    else "图片获取已停止，原有图片已保留。")
                return
            success, result = job["result"]
            if not success:
                raise NetworkError(result)
            if job["action"] == "save_lookup":
                self.save_location_results(job["game"], result, job.get("on_resolved"), job["network"])
                return
            game = self.current_artwork_game(job["game"])
            if game is None:
                return
            if job["action"] == "search":
                if "kind" in job:
                    self.kind_game_candidates(game, job["network"], result, job["kind"])
                else:
                    self.artwork_candidates(game, job["network"], result)
            elif job["action"] == "gallery":
                self.open_dialog(ArtworkChoiceDialog, game, job["network"], result, job["temporary"])
                job["gallery_owned"] = True
                self.set_status(f"请选择 {game['english_name']} 的{ARTWORK_TITLES[result['kind']]}。")
            else:
                self.publish_artwork(job, game)
                missing = result.get("missing", [])
                if job["action"] == "download_one":
                    self.operation_done(f"{game['english_name']} 的{ARTWORK_TITLES[job['value']['kind']]}已更新。")
                else:
                    self.operation_done(f"{game['english_name']} 图片获取完成。" + (
                        "缺少：" + "、".join(missing) if missing else "四类图片已保存。"))
        except Exception as exc:
            self.show_error(exc)
        finally:
            if not job.get("preserve") and not job.get("gallery_owned"):
                shutil.rmtree(job["temporary"], ignore_errors=True)

    def export_game(self):
        game = dict(self.selected_game())
        def complete(path):
            self.export_paths[game["id"]] = path
            self.operation_done(f"打包成功：{path}")
            messagebox.showinfo("打包完成", f"文件已保存：\n{path}", parent=self)
        self.run_task("正在打包全部备份和图片…", lambda: self.backups.export(game), complete)

    def import_archive(self):
        if self.busy:
            return
        filename = filedialog.askopenfilename(parent=self, title="选择游戏数据包",
                                              filetypes=[("游戏数据包", "*.zip"), ("所有文件", "*")])
        if filename:
            path = Path(filename)
            self.run_task("正在读取游戏数据包…", lambda: self.backups.inspect_import(path),
                          lambda preview: self.confirm_import(path, preview))

    def confirm_import(self, path, preview):
        game = preview["game"]
        existing = preview["existing_game"]
        action = ("合并到已有游戏，保留本机资料和目录。" if existing
                  else "新建游戏条目，并导入包中的游戏资料。")
        prompt = (f"游戏：{game['english_name']}\n备份：{preview['backup_count']} 条\n{action}\n"
                  "包中包含的图片会更新到本机。\n导入不会还原实际游戏存档；还原需要之后手动操作。\n是否导入？")
        if not messagebox.askyesno("导入游戏数据包", prompt, parent=self):
            self.set_status("已取消导入。")
            return
        def complete(result):
            self.selected_id = result["game"]["id"]
            self.refresh()
            self.tabs.set("存档备份")
            summary = f"导入完成：新增 {result['imported']} 条备份，跳过 {result['skipped']} 条重复备份。"
            if result.get("cleanup_warning"):
                summary += "\n" + result["cleanup_warning"]
            self.set_status(summary)
            messagebox.showinfo("导入完成", summary + "\n请检查游戏和存档目录，再选择需要还原的备份。", parent=self)
        self.run_task("正在校验并导入游戏数据包…", lambda: self.backups.import_game(path), complete)

    def latest_library_export(self):
        if self.library_export_path is not None and self.library_export_path.is_file():
            return self.library_export_path
        exports = self.storage.data_dir / "exports"
        paths = list(exports.glob("Library_*.zip")) if exports.exists() else []
        return max(paths, key=lambda path: path.stat().st_mtime) if paths else None

    def export_library(self):
        def complete(path):
            self.library_export_path = path
            self.operation_done(f"资料库导出完成：{path}")
            messagebox.showinfo("资料库导出完成", f"迁移包已保存：\n{path}", parent=self)
        self.run_task("正在打包整个资料库…", self.backups.export_library, complete)

    def import_library_archive(self):
        if self.busy or self._closing:
            return
        filename = filedialog.askopenfilename(parent=self, title="选择整个资料库迁移包",
                                              filetypes=[("资料库迁移包", "*.zip"), ("所有文件", "*")])
        if filename:
            path = Path(filename)
            self.run_task("正在读取资料库迁移包…", lambda: self.backups.inspect_library(path),
                          lambda preview: self.confirm_library_import(path, preview))

    def confirm_library_import(self, path, preview):
        prompt = (f"游戏：{preview['game_count']} 款（新增 {preview['new_count']}，合并 {preview['existing_count']}）\n"
                  f"备份：{preview['backup_count']} 条\n保留的无条目归档目录：{preview['orphan_count']} 个\n"
                  f"已保存配置：{preview['settings_count']} 项\n\n"
                  "已有游戏资料和路径保留，备份合并，包中同名图片会更新。\n"
                  "恢复包内已保存配置，同名设置以包内为准。\n"
                  "不会自动还原实际游戏存档。是否导入？")
        if not messagebox.askyesno("导入整个资料库", prompt, parent=self):
            self.set_status("已取消资料库导入。")
            return
        def complete(result):
            ids = {game["id"] for game in self.storage.games}
            if self.selected_id not in ids:
                self.selected_id = self.storage.games[0]["id"] if self.storage.games else None
            if "api_key" in result.get("settings_keys", []):
                self.api_key = os.environ.get("STEAMGRIDDB_API_KEY") or self.storage.settings.get("api_key", "")
            summary = (f"资料库导入完成：新增 {result['new_games']} 款游戏，合并 {result['merged_games']} 款；"
                       f"新增 {result['imported']} 条备份，跳过 {result['skipped']} 条重复备份。")
            if result.get("cleanup_warning"):
                summary += "\n" + result["cleanup_warning"]
            self.operation_done(summary)
            messagebox.showinfo("资料库导入完成", summary + "\n请检查路径，再手动还原需要使用的备份。", parent=self)
        self.run_task("正在校验并合并整个资料库…", lambda: self.backups.import_library(path), complete)

    def operation_done(self, text):
        self.refresh()
        self.set_status(text)

    def run_task(self, status, work, complete):
        if self.busy or self._closing:
            return
        self.busy = True
        self.set_status(status)
        self.set_busy_widgets()
        self.update_activity()
        def worker():
            try:
                result = work()
                self.results.put((True, result, complete))
            except Exception as exc:
                self.results.put((False, exc, complete))
        threading.Thread(target=worker, daemon=True).start()

    def set_busy_widgets(self):
        state = "disabled" if self.busy or self._closing else "normal"
        self.search.configure(state=state)
        for button in self.action_buttons + self.game_buttons + list(self.game_checks.values()) + [self.add_button, self.import_button,
                                                               self.library_button, self.settings_button]:
            button.configure(state=state)
        self.update_delete_games_button()
        if self.artwork_job is not None and hasattr(self, "artwork_button") and self.artwork_button.winfo_exists():
            self.artwork_button.configure(state="disabled")
        if self.artwork_job is not None:
            for button in self.artwork_controls:
                button.configure(state="disabled")

    def update_activity(self):
        if self.busy or self.artwork_job is not None:
            self.progress.grid(row=0, column=1, padx=(10, 0))
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.grid_remove()
        if self.artwork_job is not None:
            self.stop_artwork_button.configure(text="停止查询" if self.artwork_job["action"] == "save_lookup" else "停止获取")
            self.stop_artwork_button.configure(state="disabled" if self.artwork_job["cancelled"] else "normal")
            self.stop_artwork_button.grid(row=0, column=2, padx=(10, 0))
        else:
            self.stop_artwork_button.grid_remove()

    def poll_results(self):
        try:
            success, result, complete = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.update_activity()
            self.set_busy_widgets()
            if success:
                try:
                    complete(result)
                except Exception as exc:
                    self.show_error(exc)
            else:
                self.refresh()
                self.show_error(result)
        self.poll_artwork()
        if self._closing and self.artwork_job is None:
            self.destroy()
            return
        self.start_pending_lookup()
        self.after(100, self.poll_results)

    def close(self):
        self.save_lookup_queue.clear()
        if self.busy:
            self.stop_artwork()
            messagebox.showinfo("操作进行中", "请等待当前操作完成后再关闭，以确保数据完整。", parent=self)
        elif self.artwork_job is not None:
            self._closing = True
            self.set_busy_widgets()
            self.stop_artwork()
        else:
            self.destroy()
