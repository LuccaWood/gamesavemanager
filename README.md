# 游戏存档管理器

使用 CustomTkinter 的 Python 桌面应用。Windows 10/11 为主要运行目标，macOS 可运行源码进行调试。

## 功能

- 左侧添加、搜索、编辑、删除游戏。英文名必填，中文名、游戏本体目录、存档目录可稍后填写。
- 存档目录打包为 ZIP 快照。每条备份拥有持续递增的编号、本地日期时间（精确到秒，含时区偏移）和独立目录。
- 选择备份后可还原、复制为一条新备份、删除、打开所在目录。
- 按英文名查询 SteamGridDB，多个结果时选择对应游戏。获取 600×900 封面、920×430 横图、1920×620 标题背景、静态 Logo；某类缺图时显示提示。
- 导出所选游戏全部现有备份、图片、来源信息及游戏资料到一个 ZIP，显示路径并提供打开文件夹按钮。
- 大文件和网络操作在后台运行。操作期间禁止修改数据和关闭窗口。

## macOS 启动

项目 `.venv` 已使用 Python 3.12.12（Tk 9.0），解释器安装在 `.venv/.runtime` 中，项目依赖也已安装。在项目目录执行：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

如果自行重建环境，请选择包含 Tk 8.6 或更高版本的 Python，使用 `python -m tkinter` 检查窗口能否打开。macOS 的系统 Python/Tk 8.5 不适合作为本项目开发环境。

入口会在需要时为独立 Python 发行版补充其 Tcl/Tk 脚本路径；打包运行时沿用 PyInstaller 的资源路径。早期独立 Python 曾存在 Pillow 图片桥接问题，本项目环境已切换到修复后的版本，参见[上游修复说明](https://github.com/astral-sh/python-build-standalone/releases/tag/20250808)。

## Windows 运行和单文件打包

安装带 Tk 的 Python 3.12，并将源码复制到 Windows。macOS 的 `.venv` 不能复制到 Windows 使用；在 Windows 项目目录新建虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

源码运行正常后，构建单文件程序：

```powershell
.\.venv\Scripts\python.exe build_windows.py
```

输出为 `dist\GameSaveManager.exe`。复制这一个 EXE 到可写目录，即可在未安装 Python 的 Windows 上运行。运行时会在 EXE 旁生成 `data` 目录，已有用户数据需要连同这个目录一起移动。

构建脚本使用 PyInstaller `--onefile --windowed --collect-all customtkinter`，显式收集 CustomTkinter 的主题、字体和图标。Windows EXE 需要在 Windows 构建；本项目尚未在 Windows 实机验证打包结果。CustomTkinter 官方打包页仍推荐目录模式；单文件方案使用现代 PyInstaller 的资源收集能力，需在 Windows 验收。若需排查打包问题，可以构建目录版本：

```powershell
.\.venv\Scripts\python.exe build_windows.py --onedir
```

Windows 验收：从含中文和空格的可写目录启动，完成添加、编辑、备份、复制、还原、删除备份、下载图片、导出和打开文件夹；退出再打开，确认记录和备份仍然存在。

## SteamGridDB 设置

在 [SteamGridDB 用户偏好页面](https://www.steamgriddb.com/profile/preferences) 获取 API Key，点击左侧“SteamGridDB 设置”填写。默认仅在本次运行的内存中保留；勾选“记住”后明文存储在本机 `data/library.json`。导出包不包含设置和 API Key。也可通过 `STEAMGRIDDB_API_KEY` 环境变量提供 Key。

应用使用 [官方 API](https://www.steamgriddb.com/api/v2)，按精确尺寸选择静态 PNG/JPEG，Logo 使用 PNG。图片验证实际尺寸、格式和完整性后保存，失败时保留上次下载结果。没有 API Key 时，存档管理和打包仍可正常使用。

图片来源和作者保存在 `artwork/assets.json`。这些图片来自第三方作者，文件记录来源信息，不代表授予额外使用许可。

## 数据与还原规则

源码运行的数据保存在 `main.py` 旁，打包运行的数据保存在 EXE 旁，均不依赖启动时的工作目录：

```text
data/
  library.json                  游戏条目与设置
  games/<游戏ID>/
    sequence.json               持续递增的序号
    backups/
      000001_20261004_143000/
        metadata.json           本地时间、序号、类型与大小
        save.zip                存档目录快照
    artwork/
      assets.json               图片尺寸、来源和作者
      cover.png / wide.jpg / hero.png / logo.png
  exports/<英文名>_<游戏ID>_<时间>_<唯一标识>.zip
```

图片扩展名以实际下载格式为准。删除备份后序号不复用；失败的备份也可能使序号出现间隔。

还原按完整目录替换处理：备份中没有的旧文件会被移除。请先关闭游戏。应用先在目标同级临时目录解包并校验，再为当前存档创建“还原前保护”备份，最后替换目录。目录替换失败时尝试恢复原目录；若系统阻止回滚，会明确显示原目录保留位置。

存档位置必须是目录。为避免递归备份和覆盖程序，拒绝磁盘根目录、用户主目录、应用所在目录及其父目录、与数据目录存在包含关系的目录、符号链接与 Windows 目录联接。ZIP 路径必须兼容 Windows；越界路径、重复路径、大小写冲突和特殊文件会被拒绝。目录可包含中文、空格；支持 `~` 和系统环境变量，例如 Windows 的 `%USERPROFILE%`。

删除游戏只移除列表记录，原游戏存档不受影响，其备份和图片仍保留在 `data/games/<游戏ID>`。删除备份会永久移除对应备份文件夹。

导出包含此游戏的 `game.json`、`backups/` 与 `artwork/`。ZIP 可由常见解压软件读取，便于冷盘保存；游戏本体不进入包内。

## 验证与代码位置

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用临时目录和模拟 HTTP 响应，覆盖持久化、真实备份/完整还原、复制/删除、失败回滚、ZIP 校验、导出包内容、API 请求和图片事务写入，无需 API Key。

在可用桌面会话中启用 GUI 集成测试（macOS）：

```bash
GAME_MANAGER_GUI_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

Windows PowerShell：

```powershell
$env:GAME_MANAGER_GUI_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

GUI 测试覆盖真实窗口、表单、后台按钮流程、还原保护、导出、忙碌状态和损坏记录提示。普通测试默认跳过需要桌面会话的部分。

本机已验证：42 项测试全部通过，包含 5 项真实 GUI 集成测试；四类图片可在界面中显示，依赖检查通过。SteamGridDB 真实联网下载需自行填写 API Key 后验证；Windows EXE 仍需在 Windows 构建和验收。

- `main.py`：入口与持久数据目录。
- `game_manager/storage.py`：游戏资料与设置。
- `game_manager/backups.py`：备份、还原、复制、删除与导出。
- `game_manager/artwork.py`：SteamGridDB 查询与图片下载。
- `game_manager/ui.py`：界面与后台任务。
- `build_windows.py`：Windows 打包。

参考：[CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)、[CustomTkinter 打包说明](https://customtkinter.tomschimansky.com/documentation/packaging/)、[PyInstaller 参数](https://pyinstaller.org/en/stable/usage.html)、[PyInstaller 运行时路径](https://pyinstaller.org/en/stable/runtime-information.html)。
