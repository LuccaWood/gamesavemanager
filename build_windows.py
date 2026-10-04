"""在 Windows 上构建带 CustomTkinter 资源的可执行文件。"""
from pathlib import Path
import argparse
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="构建游戏存档管理器")
    parser.add_argument("--onedir", action="store_true", help="构建目录版本，方便排查打包问题")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows 可执行文件需要在 Windows 上构建。")
    project = Path(__file__).resolve().parent
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--onedir" if args.onedir else "--onefile", "--windowed",
         "--name", "GameSaveManager", "--collect-all", "customtkinter",
         str(project / "main.py")],
        cwd=project, check=True,
    )
    print("构建完成：", project / "dist" / ("GameSaveManager" if args.onedir else "GameSaveManager.exe"))


if __name__ == "__main__":
    main()
