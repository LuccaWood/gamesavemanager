"""游戏存档管理器。"""


def configure_tk_runtime() -> None:
    """帮助独立 Python 发行版在虚拟环境中找到其 Tcl/Tk 脚本。"""
    import os
    from pathlib import Path
    import sys
    import _tkinter

    if getattr(sys, "frozen", False):
        return
    for variable, folder, script in (
        ("TCL_LIBRARY", f"tcl{_tkinter.TCL_VERSION}", "init.tcl"),
        ("TK_LIBRARY", f"tk{_tkinter.TK_VERSION}", "tk.tcl"),
    ):
        path = Path(sys.base_prefix) / "lib" / folder
        if (path / script).is_file():
            os.environ.setdefault(variable, str(path))
