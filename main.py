# -*- coding: utf-8 -*-
"""Win11 Dash 入口（自动提权管理员以读取硬件传感器）"""
import ctypes
import os
import sys
import traceback

# 打包为 exe 时，若未管理员则自重启（比 manifest 更可靠）
if getattr(sys, "frozen", False):
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, "", None, 1)
            sys.exit(0)
    except Exception:
        pass
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "dash_error.log")


def main():
    try:
        import dashboard
        dashboard.main()
    except Exception:
        err = traceback.format_exc()
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(err + "\n")
        except Exception:
            pass
        print(err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
