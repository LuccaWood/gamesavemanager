# 游戏存档管理器

使用 CustomTkinter 的 Python 桌面应用。Windows 10/11 为主要运行目标，macOS 可运行源码进行调试。

## 功能

- 左侧添加、搜索、编辑、删除游戏。英文名必填，中文名、游戏本体目录、存档目录可稍后填写。
- 存档目录留空时，按英文名从 PCGamingWiki 查询 Windows 存档目录；唯一可用目录自动填写，多条或不完整路径显示候选。
- 存档目录打包为 ZIP 快照。每条备份拥有持续递增的编号、本地日期时间（精确到秒，含时区偏移）和独立目录。
- 选择备份后可还原、复制为一条新备份、打开所在目录；支持多选并一次删除多条备份。
- 按英文名查询 SteamGridDB，多个结果时选择对应游戏。获取 600×900 封面、920×430 横图、1920×620 标题背景、静态 Logo；某类缺图时显示提示。
- 软件全局 HTTP 代理同时用于 SteamGridDB 和 PCGamingWiki；去除勾选后本机直连。查询和下载在独立后台进程中运行，可手动停止。
- 游戏详情顶部的标题背景按原比例完整显示，随窗口宽度缩放，保留必要空白。
- 导出所选游戏全部现有备份、图片、来源信息及游戏资料到一个 ZIP，显示路径并提供打开文件夹按钮。
- 导入游戏数据包，恢复游戏条目、备份和图片；已有游戏合并归档，保留本机资料和目录配置。
- 全库导入和导出：一个 ZIP 迁移所有游戏、备份、图片、持续序号及已保存配置，也保留删除游戏条目后留下的归档。
- 图片获取期间可切换游戏、修改资料和操作存档；存档备份、还原、导入和导出期间暂时禁止修改数据和关闭窗口。

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

Windows 验收：从含中文和空格的可写目录启动，完成添加、编辑、备份、复制、还原、批量删除备份、下载图片、单游戏及全库导入导出和打开文件夹；检查还原后备份数量和序号不变，取消批量删除后记录保留；全库包分别导入空库和已有库，检查配置、路径和重复记录；使用 `%LOCALAPPDATA%` 存档路径备份含多层子目录的存档；验证留空目录后的 PCGamingWiki 查询、多目录选择、两站代理开关、后台查询或下载时切换游戏和手动停止；退出再打开，确认记录和备份仍然存在。

## 软件设置

点击左侧“软件设置”管理 API Key 和全局代理。在 [SteamGridDB 用户偏好页面](https://www.steamgriddb.com/profile/preferences) 获取 API Key 后填写。默认仅在本次运行的内存中保留；勾选“记住”后明文存储在本机 `data/library.json`。单游戏包不包含设置和 API Key，全库迁移包包含已保存配置。也可通过 `STEAMGRIDDB_API_KEY` 环境变量提供 Key。

图片获取使用 [SteamGridDB 官方 API](https://www.steamgriddb.com/api/v2)，按精确尺寸选择静态 PNG/JPEG，Logo 使用 PNG。图片验证实际尺寸、格式和完整性后保存，失败时保留上次下载结果。没有 API Key 时，存档管理、PCGamingWiki 查询和打包仍可正常使用。

在“软件设置”勾选“为软件联网请求使用 HTTP 代理”，填写完整地址，例如 `http://127.0.0.1:7890`。SteamGridDB 查询、图片下载及 PCGamingWiki 查询共用该代理；去除勾选后本机直连，不读取系统或 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理。可使用 `http://用户名:密码@主机:端口` 格式。代理设置明文保存在 `data/library.json`，不进入单游戏包，会随全库迁移包保存；去除勾选仍保留已填写的地址。

也可在关闭软件后手动修改 `library.json` 中的 `settings`，保留其他原有设置：

```json
"proxy_enabled": true,
"proxy_url": "http://127.0.0.1:7890"
```

将 `proxy_enabled` 改为 `false` 等同去除代理勾选。正在运行的联网任务沿用启动时的配置，修改设置对下一次任务生效。已有配置文件继续使用原来的字段，无需迁移。

图片和存档目录查询共用一个独立后台进程任务槽。图片任务运行时，窗口底部显示“停止获取”，存档目录查询时显示“停止查询”，切换游戏后仍可停止。停止或关闭窗口会终止联网进程，清理临时下载，并保留原图；停止还会清空待查询目录的队列。存档操作运行中仍需等待其完成后关闭。

获取期间可以操作存档、修改资料和切换游戏。新图片先在临时目录完成校验，等正在运行的存档、导入或导出操作结束后再提交，图片和关联游戏 ID 一起保存；失败时恢复原图。导出使用当时已经保存的图片。删除正在获取图片的游戏会停止任务；修改英文名后会丢弃旧任务结果，需要重新搜索。

图片来源和作者保存在 `artwork/assets.json`。这些图片来自第三方作者，文件记录来源信息，不代表授予额外使用许可。

## 自动查询存档目录

添加或编辑游戏后保存时、打开或选中游戏时，存档目录留空会按英文名查询 PCGamingWiki。已有手填或已保存的目录不会重新查询或被覆盖。点击“立即备份”或“还原所选”时目录仍为空，也会先查询，确定目录后再操作原游戏及原备份记录。原样保存 `%LOCALAPPDATA%` 等 Windows 环境变量，不在 macOS 调试时展开。

查询使用 [PCGamingWiki MediaWiki API](https://www.pcgamingwiki.com/wiki/API) 的文章内容，只解析 “Save game data location” 中的 Windows 及对应商店目录。唯一且可用的目录自动写入 `library.json`；多目录需要选择。含 `<Steam-folder>`、`<user-id>` 等占位符、注册表位置或不明确路径的候选只能查看，并通过“手动编辑目录”填写。页面列出的存档文件会转为父目录，标注“文件所在目录”，备份和还原仍针对整个目录；公共父目录不会自动填写。

查询不会阻塞主界面。正在获取图片或操作存档时，新查询等待当前任务完成；手动填写目录、修改英文名或删除游戏后，旧结果会丢弃。失败或停止后可点击存档目录旁的“查询目录”重试，也可直接编辑资料填写。英文名未精确匹配、没有 Windows 目录、超时或访问受限时保留空目录并显示提示，不反复自动请求。

本次开发环境直连 PCGamingWiki 返回 HTTP 403，尚未成功验证实站查询。程序会明确提示拒绝访问；可使用全局 HTTP 代理重试，或手动打开网站填写目录。解析、代理和界面流程已用离线响应及后台进程测试验证。

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

还原按完整目录替换处理：备份中没有的旧文件会被移除。请先关闭游戏。应用先在目标同级临时目录解包并校验，再用选中备份直接替换存档目录，不生成额外保护备份，也不增加备份序号。目录替换失败时尝试恢复原目录；若系统阻止回滚，会明确显示原目录保留位置。成功替换后若旧目录清理失败，也会显示其保留路径。

存档位置必须是目录。为避免递归备份和覆盖程序，拒绝磁盘根目录、用户主目录、应用所在目录及其父目录、与数据目录存在包含关系的目录、符号链接与 Windows 目录联接。ZIP 路径必须兼容 Windows；越界路径、重复路径、大小写冲突和特殊文件会被拒绝。目录可包含中文、空格；支持 `~` 和系统环境变量，例如 Windows 的 `%LOCALAPPDATA%`。Windows 备份时生成的 ZIP 路径会规范化为正斜杠，正常的多层子目录可备份和还原。

删除游戏只移除列表记录，原游戏存档不受影响，其备份和图片仍保留在 `data/games/<游戏ID>`。删除备份会永久移除对应备份文件夹。按住 Ctrl（macOS 为 ⌘）点选多条，或用 Shift 连选，然后点击“删除所选”，一次确认后删除全部选中记录；未选中记录保留。批量删除会先校验所有选中记录，执行中若某条删除失败，会停止后续删除，提示已删除数量和失败记录，并刷新列表。还原、复制和打开目录要求只选择一条备份。

导出包含此游戏的 `game.json`、`backups/` 与 `artwork/`。ZIP 可由常见解压软件读取，便于冷盘保存；游戏本体不进入包内。

## 导入游戏数据包

点击左侧“导入游戏数据包”，或右侧“数据打包”中的“从 ZIP 导入游戏数据包”，选择本软件导出的 ZIP。界面显示游戏名称和备份数量，确认后导入。

- 新游戏读取包中的游戏资料、全部备份和图片。换电脑后，请检查并修改游戏本体目录和存档目录。
- 游戏 ID 相同或英文名相同（不区分大小写）时合并到已有游戏，保留本机全部资料和路径。若 ID 和英文名分别对应两个游戏，拒绝导入并提示。
- 本机备份保留；导入记录的编号冲突时分配新编号，保留原备份时间。重复导入同一条来源记录会跳过，手动复制的备份仍作为独立记录保留。
- 包中的同名图片更新本机文件，保留未被覆盖的本机图片和来源信息。
- 导入先校验外层 ZIP、内部存档 ZIP 和元数据，再写入本机归档；校验或写入失败时回滚。

导入不会自动覆盖实际游戏存档。完成后选择需要的备份，再点击“还原所选”执行还原。早期导出的包也可导入，API Key 和应用设置不进入单游戏导入或导出包。

## 整个资料库迁移

在原电脑点击左侧“资料库迁移”→“导出整个资料库”。应用在 `data/exports` 生成 `Library_*.zip`，显示完整路径，也可从迁移窗口打开导出目录。将程序和这个 ZIP 复制到另一台电脑，启动后点击“资料库迁移”→“导入整个资料库”。空资料库也能使用此入口。

全库包包含 `library.json` 和 `games/` 中已保存的游戏资料、备份、图片来源、图片和序号文件。删除条目后保留的归档目录也会迁移；历史导出 ZIP 和正在进行的临时下载不重复打包。游戏本体仍使用各电脑原有的安装目录。

导入前显示新增与合并游戏数、备份数和配置数。已有游戏按 ID 或英文名合并，保留目标电脑已有资料和路径；新增游戏保留包中的路径字符串。备份按单游戏导入规则合并和去重，序号同时保留两边的递增计数；未被覆盖的本机图片保留。若身份匹配存在歧义，整库导入会拒绝。

已保存的 API Key、HTTP 代理配置和其他设置都会迁移，同名设置以包内值为准，目标电脑独有设置保留。只在内存或环境变量中使用的 API Key 不进入包；目标电脑的 `STEAMGRIDDB_API_KEY` 环境变量仍优先。迁移包中的已保存 Key 和代理密码为明文。

整个包先完成校验并准备合并结果，再一次性替换资源和配置；某个游戏损坏或写入失败时回滚，不保留部分导入结果。导入不会自动还原实际游戏存档。换电脑后先检查游戏与存档目录，固定路径可能需要修改，环境变量路径会在目标电脑操作时展开，再选择备份还原。

## 验证与代码位置

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用临时目录、模拟 HTTP 响应和本机测试代理，覆盖持久化、真实备份/完整还原、复制/删除、失败回滚、Windows ZIP 路径、ZIP 校验、单游戏与全库导入导出、冲突合并和重复导入、配置与孤留归档迁移、整库失败回滚、API 请求、代理/直连传输、图片事务写入及 PCGamingWiki Windows 目录解析、占位符和错误提示，无需 API Key 或外网。

在可用桌面会话中启用 GUI 集成测试（macOS）：

```bash
GAME_MANAGER_GUI_TESTS=1 .venv/bin/python -m unittest discover -s tests -v
```

Windows PowerShell：

```powershell
$env:GAME_MANAGER_GUI_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

GUI 测试覆盖真实窗口、表单、后台按钮流程、还原直接覆盖、多选删除及取消、部分删除失败后列表刷新、单游戏与全库导入导出、忙碌状态、损坏记录提示，以及窗口缩放和高 DPI 下顶部图片的完整显示。联网进程测试覆盖请求等待时停止、成功消息到达后取消、异常退出、启动失败、配置写入回滚、并行备份、修改资料、切换游戏和关闭窗口；存档查询测试还覆盖自动填入并持久化、多目录选择、手填优先、查询队列、取消重试，以及查询后备份还原绑定原游戏。普通测试默认跳过需要桌面会话的部分。

本机已验证：136 项测试全部通过，包含 43 项真实 GUI 集成测试；还原不增加备份或序号、失败回滚、多选删除及错误提示通过回归；PCGamingWiki 解析和全局代理逻辑、查询后备份还原、取消及手填优先已验证，原有备份、图片和全库迁移回归通过。SteamGridDB 真实联网下载需自行填写 API Key 后验证；PCGamingWiki 实站查询受 HTTP 403 限制；Windows EXE 仍需在 Windows 构建和验收。

- `main.py`：入口与持久数据目录。
- `game_manager/storage.py`：游戏资料与设置。
- `game_manager/backups.py`：备份、还原、复制、删除、导出与导入。
- `game_manager/artwork.py`：SteamGridDB 查询与图片下载。
- `game_manager/network.py`：软件全局代理与直连会话。
- `game_manager/pcgamingwiki.py`：Windows 存档目录查询与候选解析。
- `game_manager/ui.py`：界面与后台任务。
- `build_windows.py`：Windows 打包。

参考：[CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)、[CustomTkinter 打包说明](https://customtkinter.tomschimansky.com/documentation/packaging/)、[PyInstaller 参数](https://pyinstaller.org/en/stable/usage.html)、[PyInstaller 运行时路径](https://pyinstaller.org/en/stable/runtime-information.html)。
