# -*- coding: utf-8 -*-
"""
PawnIO 自动安装模块

温度读取依赖 PawnIO 内核驱动（LHM 0.9.6+ 已移除 WinRing0，温度必须经
PawnIO 读 MSR）。本模块在启动时检测 PawnIO 是否就绪，缺失时提取内嵌的
官方安装器（assets/PawnIO_setup.bin）并自动完成安装：
  免责声明弹窗(#32770)点确定 -> 主窗(PawnIOSetupWindowClass)最左按钮
  即 Install -> 轮询服务/设备注册表项出现 -> 收尾关闭弹窗。

Win11Dash 以 uac_admin 提权运行，子进程继承提权令牌，全程无 UAC 弹窗。
安装期间会短暂出现安装器窗口并自动点击，属预期行为。
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_u32 = ctypes.WinDLL("user32")
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                     wintypes.LPARAM)
EnumChildProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                   wintypes.LPARAM)
BM_CLICK = 0x00F5
WM_CLOSE = 0x0010

_SETUP_NAME = "Win11Dash_PawnIO_setup.exe"
_tried = False  # 每个进程只尝试一次，失败不反复弹安装器


def _get_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_text(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _click_child_buttons(parent) -> int:
    """点击 parent 下所有 Button 子控件，返回点击数"""
    n = 0

    @EnumChildProc
    def on_child(ch, lp):
        nonlocal n
        if _get_class(ch) == "Button":
            _u32.SendMessageW(ch, BM_CLICK, 0, 0)
            n += 1
        return True
    _u32.EnumChildWindows(parent, on_child, 0)
    return n


def _pawnio_ready() -> bool:
    """服务项 + PnP 设备枚举项同时存在视为已安装"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\PawnIO"):
            pass
    except OSError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Enum\ROOT\PawnIO") as k:
            if winreg.QueryInfoKey(k)[0] < 1:  # 无设备子键 = 驱动未绑定
                return False
    except OSError:
        return False
    return True


def _extract_setup():
    """提取内嵌安装器到 %TEMP%，返回路径；找不到返回 None"""
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", BASE_DIR),
                     "assets", "PawnIO_setup.bin"),
        os.path.join(BASE_DIR, "assets", "PawnIO_setup.bin"),
    ]
    src = next((p for p in candidates if os.path.isfile(p)), None)
    if src is None:
        return None
    dst = os.path.join(os.environ.get("TEMP", BASE_DIR), _SETUP_NAME)
    try:
        with open(src, "rb") as f_in, open(dst, "wb") as f_out:
            f_out.write(f_in.read())
    except OSError as e:
        print(f"[installer] 提取安装器失败: {e}")
        return None
    return dst


def _handle_disclaimer() -> bool:
    """找到并处理免责声明弹窗（#32770，标题或静态文本含 Pawn）"""
    handled = False

    @EnumWindowsProc
    def scan(hwnd, lp):
        nonlocal handled
        if not _u32.IsWindowVisible(hwnd) or _get_class(hwnd) != "#32770":
            return True
        title = _get_text(hwnd)
        if "Pawn" not in title:
            # 标题不匹配时检查静态文本内容
            found = []

            @EnumChildProc
            def on_child(ch, lp2):
                if _get_class(ch) == "Static" and "Pawn" in _get_text(ch):
                    found.append(ch)
                return True
            _u32.EnumChildWindows(hwnd, on_child, 0)
            if not found:
                return True
        n = _click_child_buttons(hwnd)
        if n:
            print(f"[installer] 免责声明弹窗：点击 {n} 个按钮")
            handled = True
        return True
    _u32.EnumWindows(scan, 0)
    return handled


def _find_install_button():
    """主窗（PawnIOSetupWindowClass）最左侧 Button = Install"""
    btns = []

    @EnumWindowsProc
    def on_window(hwnd, lp):
        if _u32.IsWindowVisible(hwnd) \
                and _get_class(hwnd) == "PawnIOSetupWindowClass":

            @EnumChildProc
            def on_child(ch, lp2):
                if _get_class(ch) == "Button":
                    r = wintypes.RECT()
                    _u32.GetWindowRect(ch, ctypes.byref(r))
                    btns.append((r.left, ch))
                return True
            _u32.EnumChildWindows(hwnd, on_child, 0)
        return True
    _u32.EnumWindows(on_window, 0)
    return min(btns)[1] if btns else None


def _close_pawnio_dialogs():
    """关闭安装过程中弹出的错误/完成消息框"""
    @EnumWindowsProc
    def closer(hwnd, lp):
        if _u32.IsWindowVisible(hwnd) and _get_class(hwnd) == "#32770":
            if "PawnIO" in _get_text(hwnd):
                _u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True
    _u32.EnumWindows(closer, 0)


def _auto_install(setup_path) -> bool:
    """运行安装器并窗口自动化，返回是否安装成功"""
    import subprocess
    subprocess.Popen([setup_path])

    # 阶段 1：免责声明（最多等 15s）
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.8)
        if _handle_disclaimer():
            break
    time.sleep(1)

    # 阶段 2：主窗最左按钮 = Install
    btn = _find_install_button()
    if btn is None:
        print("[installer] 未找到安装主窗")
    else:
        _u32.SendMessageW(btn, BM_CLICK, 0, 0)
        print("[installer] 已点击 Install")

    # 阶段 3：轮询就绪（最多 90s），随手关闭弹窗
    deadline = time.time() + 90
    while time.time() < deadline:
        if _pawnio_ready():
            return True
        _close_pawnio_dialogs()
        time.sleep(1.5)
    return _pawnio_ready()


def ensure_pawnio() -> bool:
    """确保 PawnIO 就绪；缺失时自动安装。返回是否就绪"""
    global _tried
    if _tried:
        return _pawnio_ready()
    _tried = True
    if _pawnio_ready():
        return True

    setup = _extract_setup()
    if setup is None:
        print("[installer] 未找到内嵌 PawnIO 安装器")
        return False
    print("[installer] 检测到 PawnIO 缺失，开始自动安装…")
    try:
        ok = _auto_install(setup)
    except Exception as e:
        print(f"[installer] 自动安装异常: {e}")
        ok = False
    finally:
        try:
            os.remove(setup)
        except OSError:
            pass
    print(f"[installer] PawnIO 自动安装{'成功' if ok else '失败'}")
    return ok
