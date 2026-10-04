from pathlib import Path
import sys


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> None:
    from game_manager.ui import GameManagerApp

    try:
        app = GameManagerApp(application_directory() / "data")
    except (ValueError, OSError) as exc:
        print(f"无法启动游戏存档管理器：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    app.mainloop()


if __name__ == "__main__":
    main()
