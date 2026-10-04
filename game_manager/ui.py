from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from . import configure_tk_runtime

configure_tk_runtime()

from tkinter import filedialog, messagebox, ttk
import webbrowser

import customtkinter as ctk
from PIL import Image, ImageOps

from .artwork import SteamGridDB
from .backups import BackupManager
from .storage import Storage


BG = "#11151e"
PANEL = "#1a2130"
MUTED = "#96a4b9"
ACCENT = "#5b83f6"


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
        self.title("编辑游戏" if game else "添加游戏")
        self.geometry("660x480")
        self.resizable(False, False)
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text="游戏资料", font=ctk.CTkFont(size=23, weight="bold")).grid(
            row=0, column=0, columnspan=3, padx=24, pady=(22, 12), sticky="w")
        self.entries = {}
        fields = [("english_name", "英文名 *"), ("chinese_name", "中文名"),
                  ("game_path", "游戏本体目录"), ("save_path", "存档目录")]
        for row, (key, label) in enumerate(fields, 1):
            ctk.CTkLabel(self, text=label).grid(row=row, column=0, padx=(24, 12), pady=10, sticky="w")
            entry = ctk.CTkEntry(self, height=38)
            entry.insert(0, self.game.get(key, ""))
            entry.grid(row=row, column=1, columnspan=1 if key.endswith("path") else 2,
                       padx=(0, 24), pady=10, sticky="ew")
            self.entries[key] = entry
            if key.endswith("path"):
                ctk.CTkButton(self, text="浏览…", width=74, command=lambda e=entry: self.browse(e)).grid(
                    row=row, column=2, padx=(0, 24), pady=10)
        ctk.CTkLabel(self, text="仅英文名必填。存档目录可稍后填写，支持 ~ 和系统环境变量。",
                     text_color=MUTED).grid(row=5, column=0, columnspan=3, padx=24, pady=8, sticky="w")
        self.error = ctk.CTkLabel(self, text="", text_color="#ff9d9d", wraplength=600)
        self.error.grid(row=6, column=0, columnspan=3, padx=24, sticky="w")
        ctk.CTkButton(self, text="保存游戏", height=40, command=self.save).grid(
            row=7, column=1, columnspan=2, padx=24, pady=18, sticky="e")
        self.bind("<Return>", lambda _: self.save())
        self.bind("<Escape>", lambda _: self.destroy())
        self.after(100, self.activate)

    def activate(self):
        self.grab_set()
        self.entries["english_name"].focus_set()

    def browse(self, entry):
        selected = filedialog.askdirectory(parent=self, title="选择目录")
        if selected:
            entry.delete(0, "end")
            entry.insert(0, selected)

    def save(self):
        values = dict(self.game)
        values.update({key: entry.get().strip() for key, entry in self.entries.items()})
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


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp):
        super().__init__(app)
        self.app = app
        self.title("SteamGridDB 设置")
        self.geometry("590x350")
        self.resizable(False, False)
        self.transient(app)
        ctk.CTkLabel(self, text="连接 SteamGridDB", font=ctk.CTkFont(size=23, weight="bold")).pack(
            anchor="w", padx=24, pady=(24, 12))
        ctk.CTkLabel(self, text="填写 API Key 后可按英文名获取游戏装饰图片。", text_color=MUTED).pack(
            anchor="w", padx=24)
        self.key = ctk.CTkEntry(self, show="•", height=40)
        self.key.insert(0, app.api_key)
        self.key.pack(fill="x", padx=24, pady=14)
        self.remember = ctk.CTkCheckBox(self, text="记住 API Key（明文保存在本机，不进入导出包）")
        if app.storage.settings.get("api_key"):
            self.remember.select()
        self.remember.pack(anchor="w", padx=24)
        ctk.CTkButton(self, text="打开 API Key 申请页面 ↗", fg_color="transparent", border_width=1,
                      command=lambda: webbrowser.open("https://www.steamgriddb.com/profile/preferences")).pack(
            anchor="w", padx=24, pady=18)
        ctk.CTkButton(self, text="保存设置", command=self.save, height=38).pack(anchor="e", padx=24)
        self.after(100, self.grab_set)

    def save(self):
        key = self.key.get().strip()
        try:
            self.app.storage.update_settings({"api_key": key if self.remember.get() else ""})
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.app.api_key = key
        self.app.set_status("SteamGridDB 设置已保存。")
        self.destroy()


class GameChoiceDialog(ctk.CTkToplevel):
    def __init__(self, app: GameManagerApp, candidates: list[dict], callback):
        super().__init__(app)
        self.title("选择 SteamGridDB 游戏")
        self.geometry("560x470")
        self.transient(app)
        ctk.CTkLabel(self, text="请选择对应游戏", font=ctk.CTkFont(size=21, weight="bold")).pack(
            anchor="w", padx=20, pady=18)
        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        for candidate in candidates:
            ctk.CTkButton(frame, text=f"{candidate['name']}  ·  ID {candidate['id']}", height=42,
                          anchor="w", command=lambda item=candidate: self.choose(item, callback)).pack(
                fill="x", pady=5)
        self.after(100, self.grab_set)

    def choose(self, candidate, callback):
        self.destroy()
        callback(candidate)


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
        self.busy = False
        self.results = queue.Queue()
        self.action_buttons = []
        self.game_buttons = []
        self.images = []
        self.export_paths = {}
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
        self.add_button = ctk.CTkButton(self.sidebar, text="＋ 添加游戏", height=42, command=self.add_game)
        self.add_button.grid(row=4, column=0, padx=16, pady=(16, 8), sticky="ew")
        self.settings_button = ctk.CTkButton(self.sidebar, text="SteamGridDB 设置", fg_color="transparent",
                                            border_width=1, command=self.settings)
        self.settings_button.grid(row=5, column=0, padx=16, pady=(0, 20), sticky="ew")
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
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.after(100, self.poll_results)

    def set_status(self, text):
        self.status.configure(text=text)

    def selected_game(self):
        return self.storage.get_game(self.selected_id) if self.selected_id else None

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
        query = self.search.get().strip().casefold()
        for game in self.storage.games:
            if query and query not in f"{game['english_name']} {game.get('chinese_name', '')}".casefold():
                continue
            text = game.get("chinese_name") or game["english_name"]
            if game.get("chinese_name"):
                text += f"\n{game['english_name']}"
            button = ctk.CTkButton(self.game_list, text=text, anchor="w", height=60,
                                   fg_color=ACCENT if game["id"] == self.selected_id else "transparent",
                                   hover_color="#2c3750", state="disabled" if self.busy else "normal",
                                   command=lambda gid=game["id"]: self.select(gid))
            button.pack(fill="x", pady=4)
            self.game_buttons.append(button)

    def select(self, game_id):
        if self.busy:
            return
        self.selected_id = game_id
        self.refresh()

    def refresh(self):
        current_tab = self.tabs.get() if hasattr(self, "tabs") and self.tabs.winfo_exists() else "存档备份"
        self.refresh_sidebar()
        for widget in self.content.winfo_children():
            widget.destroy()
        self.action_buttons = []
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
        self.button(header, "删除游戏", self.delete_game, width=92,
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
        tabs = ctk.CTkTabview(self.content, fg_color=PANEL)
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

    def preview(self, path, size, crop=False):
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
            self.images.append(result)
            return result
        except (OSError, ValueError):
            return None

    def render_banner(self, game):
        asset = self.artwork_manifest(game).get("assets", {}).get("hero", {})
        path = self.storage.artwork_dir(game) / asset.get("file", "hero.png")
        image = self.preview(path, (780, 150), crop=True) if path.is_file() else None
        banner = ctk.CTkLabel(self.content, text="" if image else "每一次冒险，都有一份归档。",
                             image=image, height=150, fg_color=PANEL, corner_radius=12,
                             font=ctk.CTkFont(size=21), text_color="#b9c9ef")
        banner.grid(row=1, column=0, sticky="ew")

    def render_backups(self, parent, game):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        self.button(bar, "＋ 立即备份", self.create_backup, width=130).pack(side="left")
        self.button(bar, "打开备份目录", lambda: self.open_game_data(game, "backups"),
                    width=130, fg_color="#303b51").pack(side="left", padx=10)
        ctk.CTkLabel(bar, text="还原会先备份当前存档，再完整替换目录。", text_color=MUTED).pack(side="right")
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
        self.backup_table = ttk.Treeview(table_frame, columns=("sequence", "time", "reason", "size"),
                                         show="headings", selectmode="browse", style="Saves.Treeview")
        for key, text, width in (("sequence", "编号", 65), ("time", "本地备份时间", 210),
                                 ("reason", "类型", 110), ("size", "压缩大小", 95)):
            self.backup_table.heading(key, text=text)
            self.backup_table.column(key, width=width, minwidth=60, stretch=key == "time")
        self.backup_table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.backup_table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.backup_table.configure(yscrollcommand=scrollbar.set)
        reasons = {"manual": "手动备份", "before_restore": "还原前保护", "copy": "复制备份"}
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
                f"#{backup['sequence']:06d}", time, reasons.get(backup.get("reason"), backup.get("reason", "")),
                readable_size(backup["size"])))
        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=2, column=0, padx=12, pady=14, sticky="ew")
        for text, callback in (("还原所选", self.restore_backup), ("复制备份", self.copy_backup),
                               ("打开所选目录", self.open_backup), ("删除备份", self.delete_backup)):
            self.button(actions, text, callback, width=118, fg_color="#303b51").pack(side="left", padx=(0, 8))

    def render_artwork(self, parent, game):
        parent.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        self.button(bar, "搜索并获取图片", self.search_artwork, width=145).pack(side="left")
        self.button(bar, "打开图片目录", lambda: self.open_game_data(game, "artwork"),
                    width=125, fg_color="#303b51").pack(side="left", padx=10)
        self.button(bar, "查看网站 ↗", lambda: webbrowser.open(
            f"https://www.steamgriddb.com/game/{game['steamgrid_id']}" if game.get("steamgrid_id")
            else "https://www.steamgriddb.com"), width=110, fg_color="#303b51").pack(side="left")
        ctk.CTkLabel(parent, text="按指定尺寸获取静态图片；缺图会保留提示。搜索同名游戏时可选择匹配结果。",
                     text_color=MUTED, wraplength=750).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 10))
        cards = ctk.CTkFrame(parent, fg_color="transparent")
        cards.grid(row=2, column=0, sticky="ew", padx=12)
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
        if manifest.get("missing"):
            ctk.CTkLabel(parent, text="该游戏缺少图片：" + "、".join(manifest["missing"]),
                         text_color="#e4bd7e").grid(row=3, column=0, padx=12, pady=12, sticky="w")
        if manifest.get("warning"):
            ctk.CTkLabel(parent, text=manifest["warning"], text_color="#ff9d9d").grid(
                row=4, column=0, padx=12, pady=12, sticky="w")

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

    def add_game(self):
        if not self.busy:
            self.open_dialog(GameDialog)

    def settings(self):
        if not self.busy:
            self.open_dialog(SettingsDialog)

    def open_dialog(self, dialog_class, *args):
        if self.dialog is not None and self.dialog.winfo_exists():
            self.dialog.lift()
            self.dialog.focus_force()
        else:
            self.dialog = dialog_class(self, *args)

    def delete_game(self):
        game = self.selected_game()
        if messagebox.askyesno("删除游戏", f"从列表移除 {game['english_name']}？\n备份和图片会保留在数据目录中。", parent=self):
            try:
                self.storage.delete_game(game["id"])
                self.selected_id = self.storage.games[0]["id"] if self.storage.games else None
                self.refresh()
            except (OSError, ValueError) as exc:
                self.show_error(exc)

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
        return selection[0]

    def create_backup(self):
        game = dict(self.selected_game())
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
        if messagebox.askyesno("还原存档", "请先关闭游戏。\n还原会先备份当前存档，然后完整替换以下目录：\n"
                              + game["save_path"] + "\n是否继续？", parent=self):
            self.run_task("正在校验并还原存档…", lambda: self.backups.restore(game, backup_id),
                          lambda result: self.operation_done("还原完成。" + (
                              f"原存档已保护为 #{result['sequence']:06d}。" if result else "")
                              + (result.get("cleanup_warning", "") if result else "")))

    def delete_backup(self):
        backup_id = self.backup_selection()
        if backup_id and messagebox.askyesno("删除备份", "永久删除这条备份？当前游戏存档不受影响。", parent=self):
            game = dict(self.selected_game())
            self.run_task("正在删除备份…", lambda: self.backups.delete(game, backup_id),
                          lambda _: self.operation_done("备份已删除。"))

    def open_backup(self):
        backup_id = self.backup_selection()
        if backup_id:
            try:
                open_folder(self.backups.backup_dir(self.selected_game(), backup_id))
            except (ValueError, OSError) as exc:
                self.show_error(exc)

    def search_artwork(self):
        if not self.api_key:
            self.open_dialog(SettingsDialog)
            self.set_status("填写 API Key 后，再点击搜索并获取图片。")
            return
        game = dict(self.selected_game())
        client = SteamGridDB(self.api_key)
        self.run_task("正在搜索 SteamGridDB…", lambda: client.search(game["english_name"]),
                      lambda candidates: self.artwork_candidates(game, client, candidates))

    def artwork_candidates(self, game, client, candidates):
        if not candidates:
            messagebox.showinfo("没有匹配结果", "没有找到对应游戏，请检查英文名。", parent=self)
            self.set_status("SteamGridDB 未找到游戏。")
        elif len(candidates) == 1:
            self.download_artwork(game, client, candidates[0])
        else:
            self.open_dialog(GameChoiceDialog, candidates,
                             lambda candidate: self.download_artwork(game, client, candidate))

    def download_artwork(self, game, client, candidate):
        game["steamgrid_id"] = candidate["id"]
        def download():
            result = client.download_assets(candidate["id"], self.storage.artwork_dir(game))
            return result
        def complete(result):
            self.storage.save_game(game)
            missing = result.get("missing", [])
            self.operation_done("图片获取完成。" + ("缺少：" + "、".join(missing) if missing else "四类图片已保存。"))
        self.run_task(f"正在获取 {candidate['name']} 的图片…", download, complete)

    def export_game(self):
        game = dict(self.selected_game())
        def complete(path):
            self.export_paths[game["id"]] = path
            self.operation_done(f"打包成功：{path}")
            messagebox.showinfo("打包完成", f"文件已保存：\n{path}", parent=self)
        self.run_task("正在打包全部备份和图片…", lambda: self.backups.export(game), complete)

    def operation_done(self, text):
        self.refresh()
        self.set_status(text)

    def run_task(self, status, work, complete):
        if self.busy:
            return
        self.busy = True
        self.set_status(status)
        self.set_busy_widgets()
        self.progress.grid(row=0, column=1, padx=(10, 0))
        self.progress.start()
        def worker():
            try:
                result = work()
                self.results.put((True, result, complete))
            except Exception as exc:
                self.results.put((False, exc, complete))
        threading.Thread(target=worker, daemon=True).start()

    def set_busy_widgets(self):
        state = "disabled" if self.busy else "normal"
        self.search.configure(state=state)
        for button in self.action_buttons + self.game_buttons + [self.add_button, self.settings_button]:
            button.configure(state=state)

    def poll_results(self):
        try:
            success, result, complete = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.progress.stop()
            self.progress.grid_remove()
            self.set_busy_widgets()
            if success:
                try:
                    complete(result)
                except Exception as exc:
                    self.show_error(exc)
            else:
                self.refresh()
                self.show_error(result)
        self.after(100, self.poll_results)

    def close(self):
        if self.busy:
            messagebox.showinfo("操作进行中", "请等待当前操作完成后再关闭，以确保数据完整。", parent=self)
        else:
            self.destroy()
