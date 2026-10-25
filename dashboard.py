# -*- coding: utf-8 -*-
"""
Win11 Dash — 五连表桌面悬浮仪表盘（帕拉梅拉摆放，发光弧线风格）
横版（620x210）: 左→右: CPU温度(小,开口朝右) | GPU占用(中,开口朝右) | CPU占用(大,内嵌内存血槽)
                 | NPU占用(中,开口朝左) | GPU温度(小,开口朝左)
竖版（210x660）: 上→下同样五表纵向层叠，左上四行数据槽（C/D盘、WiFi、电量）
支持：横/竖布局切换、三套主题（暗黑/浅色/琥珀）、矢量无级缩放、配置持久化
"""
import ctypes
import math
import os
import subprocess
import sys
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QPainter, QColor, QFont, QLinearGradient, QPen, QBrush, QAction, QIcon,
    QPixmap
)

from config import load_config, save_config
from sensor import create_reader


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def C(spec) -> QColor:
    """(r, g, b[, a]) 或 QColor -> QColor（幂等）"""
    return spec if isinstance(spec, QColor) else QColor(*spec)


def temp_color(t, th) -> QColor:
    if t is None:
        return C(th["text_none"])
    for lim, rgb in th["temp"]:
        if t < lim:
            return C(rgb)


def make_temp_icon(temp, th) -> QIcon:
    """托盘温度图标（圆角方块 + 温度数字），配色跟随主题"""
    S = 64
    pm = QPixmap(S, S)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(C(th["icon_bg"]))
    p.setPen(QPen(C(th["icon_border"]), 2))
    p.drawRoundedRect(1, 1, S - 2, S - 2, 12, 12)
    txt = "--" if temp is None else f"{temp:.0f}"
    col = temp_color(temp, th)
    fs = 34 if len(txt) <= 2 else 26
    p.setFont(QFont("Segoe UI", fs, QFont.Weight.Bold))
    p.setPen(col)
    p.drawText(QRectF(0, -4, S, S), Qt.AlignCenter, txt)
    p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    p.setPen(C(th["icon_unit"]))
    p.drawText(QRectF(0, S - 22, S, 20), Qt.AlignCenter, "°C")
    p.end()
    return QIcon(pm)


def lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t))


def fmt_rate(mb):
    """磁盘速率格式化：MB/s 数值 -> 紧凑文本"""
    if mb is None:
        return "--"
    if mb >= 1000:
        return f"{mb / 1000:.1f}GB/s"
    if mb >= 10:
        return f"{mb:.0f}MB/s"
    if mb >= 1:
        return f"{mb:.1f}MB/s"
    return f"{mb * 1024:.0f}KB/s"


# ---------------------------------------------------------------------------
# 主题：暗黑（现款霓虹）/ 浅色 / 琥珀赛车
# 值为 (r,g,b[,a]) 元组，绘制时经 C() 转 QColor
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "label": "暗黑",
        "win_grad": ((13, 17, 23, 200), (22, 27, 34, 200)),
        "win_border": (48, 54, 61, 160),
        "glass": (20, 24, 32, 150),
        "ring_edge": (255, 255, 255, 26),
        "tick_hi": (255, 255, 255, 70),
        "tick_lo": (255, 255, 255, 40),
        "track_arc": (255, 255, 255, 22),
        "track_pen": (255, 255, 255, 40),
        "track_fill": (255, 255, 255, 18),
        "text_main": (240, 244, 248),
        "text_num": (240, 240, 240),
        "text_sub": (255, 255, 255, 150),
        "text_pct": (255, 255, 255, 140),
        "text_mem": (255, 220, 120),
        "text_none": (255, 255, 255, 120),
        "temp": ((60, (0, 242, 234)), (75, (255, 200, 0)),
                 (90, (255, 107, 0)), (10 ** 9, (255, 45, 85))),
        "disk_grad": ((0.0, (0, 242, 234)), (0.6, (255, 200, 0)),
                      (1.0, (255, 45, 85))),
        "side_on": (0, 242, 234),
        "side_off": (255, 255, 255, 45),
        "bat": ((20, (255, 45, 85)), (50, (255, 200, 0)),
                (10 ** 9, (0, 230, 118))),
        "ok_green": (0, 230, 118),
        "stroke": (255, 255, 255, 90),
        "icon_bg": (13, 17, 23, 240),
        "icon_border": (48, 54, 61),
        "icon_unit": (255, 255, 255, 140),
        "gauges": {
            "cpu_temp": ((0, 242, 234), (255, 45, 85)),
            "gpu_load": ((168, 85, 247), (255, 45, 85)),
            "cpu_load": ((0, 242, 234), (0, 120, 255)),
            "npu_load": ((0, 220, 255), (150, 110, 255)),
            "npu_gpu_temp": ((0, 242, 234), (255, 45, 85)),
        },
    },
    "light": {
        "label": "浅色",
        "win_grad": ((243, 246, 250, 235), (226, 233, 241, 235)),
        "win_border": (178, 188, 200, 200),
        "glass": (255, 255, 255, 150),
        "ring_edge": (30, 40, 52, 40),
        "tick_hi": (30, 40, 52, 110),
        "tick_lo": (30, 40, 52, 55),
        "track_arc": (30, 40, 52, 28),
        "track_pen": (30, 40, 52, 55),
        "track_fill": (30, 40, 52, 14),
        "text_main": (28, 34, 44),
        "text_num": (24, 30, 40),
        "text_sub": (30, 40, 52, 170),
        "text_pct": (30, 40, 52, 160),
        "text_mem": (150, 95, 0),
        "text_none": (30, 40, 52, 120),
        "temp": ((60, (0, 160, 168)), (75, (205, 140, 0)),
                 (90, (220, 80, 0)), (10 ** 9, (225, 35, 80))),
        "disk_grad": ((0.0, (0, 160, 168)), (0.6, (215, 145, 0)),
                      (1.0, (225, 45, 85))),
        "side_on": (0, 150, 160),
        "side_off": (30, 40, 52, 60),
        "bat": ((20, (225, 45, 85)), (50, (205, 145, 0)),
                (10 ** 9, (0, 160, 90))),
        "ok_green": (0, 160, 90),
        "stroke": (30, 40, 52, 120),
        "icon_bg": (245, 248, 252, 245),
        "icon_border": (170, 180, 192),
        "icon_unit": (30, 40, 52, 160),
        "gauges": {
            "cpu_temp": ((0, 160, 168), (225, 45, 85)),
            "gpu_load": ((130, 60, 220), (225, 45, 85)),
            "cpu_load": ((0, 150, 200), (0, 80, 220)),
            "npu_load": ((0, 150, 200), (110, 80, 230)),
            "npu_gpu_temp": ((0, 160, 168), (225, 45, 85)),
        },
    },
    "amber": {
        "label": "琥珀",
        "win_grad": ((26, 19, 10, 215), (40, 28, 14, 215)),
        "win_border": (122, 92, 44, 170),
        "glass": (34, 24, 12, 170),
        "ring_edge": (255, 210, 140, 40),
        "tick_hi": (255, 220, 160, 90),
        "tick_lo": (255, 220, 160, 45),
        "track_arc": (255, 220, 160, 25),
        "track_pen": (255, 220, 160, 45),
        "track_fill": (255, 220, 160, 16),
        "text_main": (255, 236, 200),
        "text_num": (255, 240, 210),
        "text_sub": (255, 220, 170, 170),
        "text_pct": (255, 220, 170, 150),
        "text_mem": (255, 200, 90),
        "text_none": (255, 230, 190, 130),
        "temp": ((60, (255, 190, 60)), (75, (255, 140, 30)),
                 (90, (255, 80, 30)), (10 ** 9, (255, 30, 60))),
        "disk_grad": ((0.0, (255, 190, 60)), (0.6, (255, 120, 30)),
                      (1.0, (255, 40, 60))),
        "side_on": (255, 180, 70),
        "side_off": (255, 220, 160, 50),
        "bat": ((20, (255, 40, 60)), (50, (255, 160, 30)),
                (10 ** 9, (255, 200, 80))),
        "ok_green": (255, 190, 80),
        "stroke": (255, 220, 160, 110),
        "icon_bg": (30, 21, 10, 245),
        "icon_border": (122, 92, 44),
        "icon_unit": (255, 220, 170, 150),
        "gauges": {
            "cpu_temp": ((255, 190, 60), (255, 40, 70)),
            "gpu_load": ((255, 150, 40), (255, 40, 70)),
            "cpu_load": ((255, 200, 70), (255, 110, 30)),
            "npu_load": ((255, 170, 50), (255, 80, 90)),
            "npu_gpu_temp": ((255, 190, 60), (255, 40, 70)),
        },
    },
}


# ---------------------------------------------------------------------------
# 布局规格：横版 / 竖版
# 表盘元组: (key, cx, cy, r, vmin, vmax, label, unit, a0, span)
#   a0   = 0 值所在数学角（度，0=正右，逆时针为正）
#   span = 满量程扫掠角（带符号：负=顺时针，正=逆时针）
# 开口（缺口）藏入相邻表盘的遮挡区：
#   横版：左侧两表开口朝右，右侧两表开口朝左，中表开口朝下（内存血槽）
#   竖版：上两表开口朝下，下两表开口朝上，中表开口朝下（内存血槽）
# ---------------------------------------------------------------------------

GAUGES_H = [
    ("cpu_temp", 92, 133, 56, 30, 110, "CPU 温度", "°C", 285, -210),
    ("gpu_load", 192, 120, 70, 0, 100, "GPU 占用", "%", 285, -210),
    ("cpu_load", 310, 105, 92, 0, 100, "CPU 占用", "%", 225, -270),
    ("npu_load", 428, 120, 70, 0, 100, "NPU 占用", "%", 255, 210),
    ("npu_gpu_temp", 528, 133, 56, 30, 110, "GPU 温度", "°C", 255, 210),
]

GAUGES_V = [
    ("cpu_temp", 105, 118, 50, 30, 110, "CPU 温度", "°C", 195, -210),
    ("gpu_load", 105, 214, 60, 0, 100, "GPU 占用", "%", 195, -210),
    ("cpu_load", 105, 342, 80, 0, 100, "CPU 占用", "%", 225, -270),
    ("npu_load", 105, 470, 60, 0, 100, "NPU 占用", "%", 15, -210),
    ("npu_gpu_temp", 105, 558, 46, 30, 110, "GPU 温度", "°C", 15, -210),
]

LAYOUTS = {
    "h": {"label": "横版", "w": 620, "h": 210, "gauges": GAUGES_H},
    "v": {"label": "竖版", "w": 210, "h": 658, "gauges": GAUGES_V},
}

SCALE_MIN, SCALE_MAX, SCALE_STEP = 0.5, 3.0, 0.05


# ---------------------------------------------------------------------------
# 开机自启（exe 带管理员清单，Run 注册表会被 Windows 拦截 → 用任务计划）
# ---------------------------------------------------------------------------

TASK_NAME = "Win11Dash"


def _autostart_cmd():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "main.py")
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{script}"'


def _no_window():
    """GUI 子系统程序里拉控制台程序时必须加这个，否则会额外开一个控制台窗口，
    既闪黑框又慢（实测 schtasks 查询从 0.05s 变成 0.5s+）"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_autostart():
    r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                       capture_output=True, creationflags=_no_window())
    return r.returncode == 0


def set_autostart(on):
    if on:
        subprocess.run(["schtasks", "/create", "/tn", TASK_NAME,
                        "/tr", _autostart_cmd(), "/sc", "onlogon",
                        "/rl", "highest", "/f"], capture_output=True,
                       creationflags=_no_window())
    else:
        subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                       capture_output=True, creationflags=_no_window())


def arc_rect(cx, cy, r):
    return QRectF(cx - r, cy - r, r * 2, r * 2)


# ---------------------------------------------------------------------------
# 五连表控件
# ---------------------------------------------------------------------------

class ClusterWidget(QWidget):
    def __init__(self, parent, layout_key, theme_key, scale):
        super().__init__(parent)
        self._layout_key = layout_key
        self._theme_key = theme_key
        self._scale = scale
        self._th = THEMES[theme_key]
        self._gauges = LAYOUTS[layout_key]["gauges"]
        self._cur = {g[0]: None for g in self._gauges}   # None = 尚无数据
        self._tgt = {g[0]: 0.0 for g in self._gauges}
        self.extra = {"mem_used_gb": None, "mem_load": None,
                      "disk_c_rate": None, "disk_d_rate": None,
                      "wifi_pct": None, "battery_pct": None}
        # 磁盘速率自适应峰值（血槽比例尺，缓慢衰减）
        self._disk_peak = {"C": 50.0, "D": 50.0}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def set_appearance(self, layout_key, theme_key, scale):
        """布局/主题/缩放变化时由 DashWindow 调用"""
        if layout_key != self._layout_key:
            self._layout_key = layout_key
            self._gauges = LAYOUTS[layout_key]["gauges"]
        self._theme_key = theme_key
        self._th = THEMES[theme_key]
        self._scale = scale
        self.update()

    def set_value(self, key, v):
        if v is not None and key in self._tgt:
            v = float(v)
            if math.isnan(v):
                return
            # 钳到量程内（LHM 传感器值偶发超界）
            for g in self._gauges:
                if g[0] == key:
                    v = max(g[4], min(g[5], v))
                    break
            self._tgt[key] = v
            if self._cur[key] is None:
                self._cur[key] = v  # 首次直接置位

    def set_extra(self, mem_used_gb, mem_load=None, disk_c_rate=None,
                  disk_d_rate=None, wifi_pct=None, battery_pct=None):
        self.extra["mem_used_gb"] = mem_used_gb
        self.extra["mem_load"] = mem_load
        self.extra["disk_c_rate"] = disk_c_rate
        self.extra["disk_d_rate"] = disk_d_rate
        self.extra["wifi_pct"] = wifi_pct
        self.extra["battery_pct"] = battery_pct
        # 峰值缓慢衰减，让血槽对突发速率敏感
        for L, v in (("C", disk_c_rate), ("D", disk_d_rate)):
            if v is not None:
                self._disk_peak[L] = max(
                    50.0, self._disk_peak[L] * 0.98, float(v))

    def _tick(self):
        dirty = False
        for k, cur in self._cur.items():
            if cur is None:
                continue
            d = self._tgt[k] - cur
            if abs(d) > 0.002:
                self._cur[k] += d * 0.15
                dirty = True
        if dirty:
            self.update()

    # -- 绘制 -------------------------------------------------------------

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(self._scale, self._scale)   # 矢量缩放：逻辑坐标绘制
        # 先画外侧小表，后画大表（交叠层次）
        for idx in (0, 4, 1, 3, 2):
            key, cx, cy, r, vmin, vmax, label, unit, a0, span \
                = self._gauges[idx]
            cur = self._cur[key]
            ratio = 0.0 if cur is None else max(
                0.0, min(1.0, (cur - vmin) / (vmax - vmin)))
            ca, cb = (C(c) for c in self._th["gauges"][key])
            self._draw_gauge(p, key, cx, cy, r, vmin, vmax, ca, cb, label,
                             unit, a0, span, ratio, cur)
            if key == "cpu_load":
                self._draw_center_digits(p, cx, cy, r)
        if self._layout_key == "h":
            self._draw_side_widgets_h(p)
        else:
            self._draw_side_widgets_v(p)
        p.end()

    def _mini_bar(self, p, x, y, w, h, ratio, grad=None, color=None):
        """小横槽：x,y 为中心线起点，ratio 0~1"""
        th = self._th
        by = y - h / 2
        p.setPen(QPen(C(th["track_pen"]), 1))
        p.setBrush(C(th["track_fill"]))
        p.drawRoundedRect(QRectF(x, by, w, h), h / 2, h / 2)
        if ratio is None:
            return
        fw = w * max(0.0, min(1.0, ratio))
        if fw <= 1:
            return
        p.setPen(Qt.NoPen)
        if grad:
            g = QLinearGradient(x, 0, x + w, 0)
            for t, c in grad:
                g.setColorAt(t, C(c))
            p.setBrush(QBrush(g))
        else:
            p.setBrush(color or C(th["ok_green"]))
        p.drawRoundedRect(QRectF(x, by, fw, h), h / 2, h / 2)

    def _seg_bar(self, p, x, y, w, h, ratio, color):
        """方块分格槽：x,y 为中心线起点，点亮的格数随 ratio 增加"""
        th = self._th
        by = y - h / 2
        gap, seg_w = 2.5, 8.0
        n = max(1, int((w + gap) / (seg_w + gap)))
        lit = 0 if ratio is None else int(
            round(max(0.0, min(1.0, ratio)) * n))
        for i in range(n):
            sx = x + i * (seg_w + gap)
            if sx + seg_w > x + w + 0.5:
                break
            if i < lit:
                p.setPen(Qt.NoPen)
                p.setBrush(color)
            else:
                p.setPen(QPen(C(th["track_pen"]), 1))
                p.setBrush(C(th["track_fill"]))
            p.drawRoundedRect(QRectF(sx, by, seg_w, h), 2, 2)

    def _disk_grad(self):
        return [(t, C(c)) for t, c in self._th["disk_grad"]]

    def _bat_color(self, bat):
        if bat is None:
            return C(self._th["ok_green"])
        for lim, rgb in self._th["bat"]:
            if bat < lim:
                return C(rgb)

    def _draw_side_widgets_h(self, p):
        """横版：左上 C:/D: 磁盘速率横槽（C 长 D 短）；
        右上 WiFi / 电量横槽（与左侧对称，小图标作标签，数字在右）"""
        th = self._th
        disk_grad = self._disk_grad()
        # --- 左上：磁盘速率行（数字紧跟槽后；D 行下移贴合表盘留白） ---
        rows = [("C:", self.extra.get("disk_c_rate"), 100),
                ("D:", self.extra.get("disk_d_rate"), 60)]
        y = 23
        for label, val, bw in rows:
            if val is None and label == "D:":
                continue  # 无 D 盘跳过
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.setPen(C(th["text_sub"]))
            p.drawText(QRectF(28, y - 8, 18, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, label)
            ratio = None
            if val is not None:
                ratio = val / self._disk_peak[label[0]]
            self._mini_bar(p, 46, y, bw, 7, ratio, grad=disk_grad)
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.setPen(C(th["text_main"]))
            p.drawText(QRectF(46 + bw + 5, y - 8, 58, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, fmt_rate(val))
            y += 25

        # --- 右上：WiFi（长槽）/ 电量（短槽），与左侧镜像对称 ---
        # 布局（左->右）：数字(右对齐) | 分格槽 | 小图标
        wifi = self.extra.get("wifi_pct")
        bat = self.extra.get("battery_pct")
        on_c = C(th["side_on"])
        off_c = C(th["side_off"])

        # WiFi 行（y=23，长槽 100）
        if wifi is None:
            lv = 0
        elif wifi >= 70:
            lv = 3
        elif wifi >= 40:
            lv = 2
        else:
            lv = 1
        wgx, wgy = 581, 27   # 图标圆点，弧在其上方
        p.setPen(Qt.NoPen)
        p.setBrush(on_c if lv >= 1 else off_c)
        p.drawEllipse(QPointF(wgx, wgy), 1.5, 1.5)
        for i, rr in enumerate((4.0, 7.5)):
            p.setPen(QPen(on_c if lv >= i + 2 else off_c, 1.6,
                          Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawArc(arc_rect(wgx, wgy, rr), 50 * 16, 80 * 16)
        self._seg_bar(p, 474, 23, 100, 7,
                      None if wifi is None else wifi / 100.0, on_c)
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.setPen(C(th["text_main"]))
        wtxt = "--" if wifi is None else f"{wifi}%"
        p.drawText(QRectF(411, 15, 56, 16),
                   Qt.AlignRight | Qt.AlignVCenter, wtxt)

        # 电量行（y=48，短槽 60）
        bat_col = self._bat_color(bat)
        self._seg_bar(p, 514, 48, 60, 7,
                      None if bat is None else bat / 100.0, bat_col)
        # 电池小图标（含填充）
        bgx, bgy, bgw, bgh = 574, 43.5, 18, 9
        p.setPen(QPen(C(th["stroke"]), 1.1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(bgx, bgy, bgw, bgh), 2.5, 2.5)
        p.setPen(Qt.NoPen)
        p.setBrush(C(th["stroke"]))
        p.drawRoundedRect(QRectF(bgx + bgw + 1, bgy + 2.5, 2.5, 4), 1, 1)
        if bat is not None:
            fw = (bgw - 3) * max(0.0, min(1.0, bat / 100.0))
            if fw > 0.5:
                p.setBrush(bat_col)
                p.drawRoundedRect(QRectF(bgx + 1.5, bgy + 1.5, fw, bgh - 3),
                                  1.5, 1.5)
        btxt = "--" if bat is None else f"{bat}%"
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.setPen(C(th["text_main"]))
        p.drawText(QRectF(452, 40, 55, 16),
                   Qt.AlignRight | Qt.AlignVCenter, btxt)

    def _draw_side_widgets_v(self, p):
        """竖版：顶部两行 WiFi/电量，底部两行 C:/D: 磁盘速率"""
        th = self._th
        disk_grad = self._disk_grad()
        wifi = self.extra.get("wifi_pct")
        bat = self.extra.get("battery_pct")
        on_c = C(th["side_on"])
        off_c = C(th["side_off"])

        # --- WiFi 行 y=16：数字(右对齐) | 分格槽 | 图标 ---
        if wifi is None:
            lv = 0
        elif wifi >= 70:
            lv = 3
        elif wifi >= 40:
            lv = 2
        else:
            lv = 1
        wgx, wgy = 174, 20
        p.setPen(Qt.NoPen)
        p.setBrush(on_c if lv >= 1 else off_c)
        p.drawEllipse(QPointF(wgx, wgy), 1.5, 1.5)
        for i, rr in enumerate((4.0, 7.5)):
            p.setPen(QPen(on_c if lv >= i + 2 else off_c, 1.6,
                          Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawArc(arc_rect(wgx, wgy, rr), 50 * 16, 80 * 16)
        self._seg_bar(p, 62, 16, 96, 7,
                      None if wifi is None else wifi / 100.0, on_c)
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.setPen(C(th["text_main"]))
        wtxt = "--" if wifi is None else f"{wifi}%"
        p.drawText(QRectF(8, 8, 48, 16),
                   Qt.AlignRight | Qt.AlignVCenter, wtxt)

        # --- 电量行 y=38 ---
        bat_col = self._bat_color(bat)
        self._seg_bar(p, 62, 38, 60, 7,
                      None if bat is None else bat / 100.0, bat_col)
        bgx, bgy, bgw, bgh = 128, 33.5, 18, 9
        p.setPen(QPen(C(th["stroke"]), 1.1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(bgx, bgy, bgw, bgh), 2.5, 2.5)
        p.setPen(Qt.NoPen)
        p.setBrush(C(th["stroke"]))
        p.drawRoundedRect(QRectF(bgx + bgw + 1, bgy + 2.5, 2.5, 4), 1, 1)
        if bat is not None:
            fw = (bgw - 3) * max(0.0, min(1.0, bat / 100.0))
            if fw > 0.5:
                p.setBrush(bat_col)
                p.drawRoundedRect(QRectF(bgx + 1.5, bgy + 1.5, fw, bgh - 3),
                                  1.5, 1.5)
        btxt = "--" if bat is None else f"{bat}%"
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.setPen(C(th["text_main"]))
        p.drawText(QRectF(8, 30, 48, 16),
                   Qt.AlignRight | Qt.AlignVCenter, btxt)

        # --- 磁盘行 y=618 / 640（仪表最下方） ---
        rows = [("C:", self.extra.get("disk_c_rate"), 96),
                ("D:", self.extra.get("disk_d_rate"), 96)]
        y = 618
        for label, val, bw in rows:
            if val is None and label == "D:":
                continue  # 无 D 盘跳过
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.setPen(C(th["text_sub"]))
            p.drawText(QRectF(10, y - 8, 18, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, label)
            ratio = None
            if val is not None:
                ratio = val / self._disk_peak[label[0]]
            self._mini_bar(p, 30, y, bw, 7, ratio, grad=disk_grad)
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.setPen(C(th["text_main"]))
            p.drawText(QRectF(30 + bw + 5, y - 8, 62, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, fmt_rate(val))
            y += 22

    def _draw_gauge(self, p, key, cx, cy, r, vmin, vmax, ca, cb, label, unit,
                    a0_deg, span_deg, ratio, raw):
        th = self._th
        arc_r = r - 8
        pen_w = max(5.0, r * 0.11)
        start_q = int(a0_deg * 16)          # Qt 起始角（1/16 度）
        span_q = int(span_deg * 16)         # Qt 扫掠角（带符号）

        # 玻璃底
        p.setPen(Qt.NoPen)
        p.setBrush(C(th["glass"]))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(C(th["ring_edge"]), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r - 1, r - 1)

        # 刻度点（沿扇区均匀分布）
        n = 12
        for i in range(n + 1):
            a_deg = a0_deg + span_deg * i / n
            a = math.radians(a_deg)
            r_out = r - 2
            r_in = r - (7 if i % 3 == 0 else 5)
            p.setPen(QPen(C(th["tick_hi"] if i % 3 == 0 else th["tick_lo"]),
                          1.4 if i % 3 == 0 else 1))
            p.drawLine(QPointF(cx + r_in * math.cos(a), cy - r_in * math.sin(a)),
                       QPointF(cx + r_out * math.cos(a), cy - r_out * math.sin(a)))

        # 背景弧（暗）
        pen = QPen(C(th["track_arc"]), pen_w, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(arc_rect(cx, cy, arc_r), start_q, span_q)

        # 前景发光弧：分段渐变
        if ratio > 0.003:
            segs = 40
            span_total = span_deg * ratio   # 带符号
            # 发光层
            glow_pen = QPen(QColor(0, 0, 0, 0), pen_w + 5, Qt.SolidLine,
                            Qt.RoundCap)
            for i in range(segs):
                t0 = i / segs
                t1 = (i + 1) / segs
                col = lerp_color(ca, cb, t0)
                col.setAlpha(46)
                glow_pen.setColor(col)
                p.setPen(glow_pen)
                aq0 = start_q + int(16 * span_total * t0)
                aq1 = int(16 * span_total * (t1 - t0))
                if aq1 == 0:
                    aq1 = 1 if span_total > 0 else -1
                p.drawArc(arc_rect(cx, cy, arc_r), aq0, aq1)
            # 主弧
            main_pen = QPen(QColor(0, 0, 0), pen_w, Qt.SolidLine,
                            Qt.RoundCap)
            for i in range(segs):
                t0 = i / segs
                t1 = (i + 1) / segs
                main_pen.setColor(lerp_color(ca, cb, t0))
                p.setPen(main_pen)
                aq0 = start_q + int(16 * span_total * t0)
                aq1 = int(16 * span_total * (t1 - t0))
                if aq1 == 0:
                    aq1 = 1 if span_total > 0 else -1
                p.drawArc(arc_rect(cx, cy, arc_r), aq0, aq1)
            # 尖端亮点
            tip_deg = a0_deg + span_total
            a = math.radians(tip_deg)
            tx, ty = cx + arc_r * math.cos(a), cy - arc_r * math.sin(a)
            tip_col = lerp_color(ca, cb, ratio)
            p.setPen(Qt.NoPen)
            p.setBrush(tip_col)
            p.drawEllipse(QPointF(tx, ty), pen_w * 0.55, pen_w * 0.55)
            halo = QColor(tip_col)
            halo.setAlpha(70)
            p.setBrush(halo)
            p.drawEllipse(QPointF(tx, ty), pen_w * 0.95, pen_w * 0.95)

        # 中心读数（中央大表数字区单独画）
        if key != "cpu_load":
            if unit == "°C":
                vtxt = "--" if raw is None else f"{raw:.0f}"
                vcol = temp_color(raw, th)
            else:
                vtxt = "--" if raw is None else f"{raw:.0f}"
                vcol = C(th["text_num"])
            fs = max(11, int(r * 0.34))
            p.setFont(QFont("Segoe UI", fs, QFont.Weight.Bold))
            p.setPen(vcol)
            p.drawText(QRectF(cx - r, cy - r * 0.22, r * 2, r * 0.55),
                       Qt.AlignCenter, vtxt)
            p.setFont(QFont("Microsoft YaHei", max(7, int(r * 0.12)),
                            QFont.Weight.Bold))
            p.setPen(C(th["text_sub"]))
            p.drawText(QRectF(cx - r, cy + r * 0.36, r * 2, r * 0.22),
                       Qt.AlignCenter, f"{label} {unit}")

    def _draw_center_digits(self, p, cx, cy, r):
        """中央大表：CPU 占用大数字（右下角小 %） + 内存血槽（数字在血槽上方）"""
        th = self._th
        load = self._cur["cpu_load"]
        load_txt = "--" if load is None else f"{load:.0f}"
        num_font = QFont("Segoe UI", 26, QFont.Weight.Bold)
        p.setFont(num_font)
        p.setPen(C(th["text_main"]))
        num_rect = QRectF(cx - 70, cy - r * 0.32, 140, 36)
        p.drawText(num_rect, Qt.AlignCenter, load_txt)
        # 小 % 紧跟大数字右下角
        w_num = p.fontMetrics().horizontalAdvance(load_txt)
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.setPen(C(th["text_pct"]))
        p.drawText(QRectF(cx + w_num / 2 + 3, num_rect.bottom() - 19, 18, 16),
                   Qt.AlignLeft | Qt.AlignBottom, "%")

        # 内存血槽（下移嵌入表盘底部开口区；数字在血槽上方）
        mem_gb = self.extra["mem_used_gb"]
        mem_pct = self.extra.get("mem_load")
        bar_w, bar_h = r * 0.75, 9
        bx, by = cx - bar_w / 2, cy + r * 0.66
        # 数字行：血槽上方
        mem_txt = ("--" if mem_gb is None else f"{mem_gb:.1f}GB") \
            + ("  " if mem_pct is not None and mem_gb is not None else "") \
            + ("" if mem_pct is None else f"{mem_pct:.0f}%")
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.setPen(C(th["text_mem"]))
        p.drawText(QRectF(bx - 15, by - 14, bar_w + 30, 12),
                   Qt.AlignCenter, mem_txt)
        # 槽底
        p.setPen(QPen(C(th["track_pen"]), 1))
        p.setBrush(C(th["track_fill"]))
        p.drawRoundedRect(QRectF(bx, by, bar_w, bar_h), bar_h / 2,
                          bar_h / 2)
        # 填充
        if mem_pct is not None:
            ratio = max(0.0, min(1.0, mem_pct / 100.0))
            fw = bar_w * ratio
            if fw > 1:
                fg = QLinearGradient(bx, 0, bx + bar_w, 0)
                for t, c in self._th["disk_grad"]:
                    fg.setColorAt(t, C(c))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(fg))
                p.drawRoundedRect(QRectF(bx, by, fw, bar_h), bar_h / 2,
                                  bar_h / 2)


# ---------------------------------------------------------------------------
# 数据工作线程（纯 Python 线程 + 队列，规避 CLR 与 QThread 冲突）
# ---------------------------------------------------------------------------

class SensorWorker:
    def __init__(self):
        import queue
        import threading
        self.queue = queue.Queue(maxsize=2)
        self._running = True
        self._reader = None
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def _loop(self):
        self._reader = create_reader()
        while self._running:
            try:
                d = self._reader.read()
                if self.queue.full():
                    try:
                        self.queue.get_nowait()
                    except Exception:
                        pass
                self.queue.put(d)
            except Exception as e:
                print(f"[SensorWorker] read error: {e}")
            import time
            t0 = time.time()
            while self._running and time.time() - t0 < 1.0:
                time.sleep(0.1)

    def poll(self):
        try:
            return self.queue.get_nowait()
        except Exception:
            return None

    def stop(self):
        self._running = False
        self._thread.join(timeout=2)
        if self._reader:
            self._reader.close()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class DashWindow(QWidget):
    def __init__(self, cfg):
        super().__init__()
        self._cfg = cfg
        self._drag_pos = None
        self._last_temp = None
        self._first_hide = True
        self._menu = None
        self._scale = float(cfg.get("scale", 1.0))
        self._layout_key = cfg.get("layout", "h")
        self._theme_key = cfg.get("theme", "dark")
        self._th = THEMES[self._theme_key]
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.cluster = ClusterWidget(self, self._layout_key,
                                     self._theme_key, self._scale)

        from PySide6.QtWidgets import QPushButton
        self.btn_close = QPushButton("×", self)
        self.btn_close.clicked.connect(self.hide_to_tray)

        self._apply_layout()

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(make_temp_icon(None, self._th))
        self.tray.setToolTip("Win11 Dash")
        tray_menu = QMenu(self)
        act_show = QAction("显示 / 隐藏", self)
        act_show.triggered.connect(self._toggle_visible)
        tray_menu.addAction(act_show)
        act_auto = QAction("开机自启", self)
        act_auto.setCheckable(True)
        act_auto.setChecked(is_autostart())
        act_auto.triggered.connect(
            lambda checked: set_autostart(checked))
        tray_menu.addAction(act_auto)
        tray_menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        tray_menu.addAction(act_quit)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

        if not cfg.get("topmost", True):
            self._set_topmost(False)
        op = float(cfg.get("opacity", 1.0))
        if op < 1.0:
            self.setWindowOpacity(op)

        self.worker = SensorWorker()
        self.worker.start()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_data)
        self._poll_timer.start(300)

        # Ctrl+滚轮连续缩放时延迟保存，避免高频写盘
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_cfg)

    # --- 布局 / 主题 / 缩放 -------------------------------------------------

    def _apply_layout(self, keep_center=False):
        L = LAYOUTS[self._layout_key]
        bw, bh = L["w"], L["h"]
        s = self._scale
        old_center = self.frameGeometry().center() if keep_center else None
        self.setFixedSize(round(bw * s), round(bh * s))
        self.cluster.setGeometry(0, 0, self.width(), self.height())
        self.cluster.set_appearance(self._layout_key, self._theme_key, s)
        # 关闭按钮随缩放
        bs = max(16, round(24 * s))
        self.btn_close.setFixedSize(bs, bs)
        self.btn_close.move(self.width() - bs - 6, round(6 * s))
        self.btn_close.setStyleSheet(
            f"QPushButton {{ color: rgba(255,255,255,120); background: transparent;"
            f" border: none; font-size: {round(18 * s)}px; font-weight: bold; }}"
            f"QPushButton:hover {{ color: #ff2d55; }}")
        self.update()
        if old_center is not None:
            self.move(old_center.x() - self.width() // 2,
                      old_center.y() - self.height() // 2)

    def set_scale(self, s, from_menu=False):
        s = max(SCALE_MIN, min(SCALE_MAX, s))
        if abs(s - self._scale) < 0.001:
            return
        self._scale = s
        self._apply_layout(keep_center=not from_menu)
        self._save_timer.start(600)

    def set_theme(self, key):
        if key == self._theme_key:
            return
        self._theme_key = key
        self._th = THEMES[key]
        self.cluster.set_appearance(self._layout_key, key, self._scale)
        self.tray.setIcon(make_temp_icon(self._last_temp, self._th))
        self.update()
        self._save_cfg()

    def set_layout(self, key):
        if key == self._layout_key:
            return
        self._store_pos(self._layout_key)      # 记住旧布局的位置
        self._layout_key = key
        self._apply_layout()
        pos = self._cfg.get("pos_h" if key == "h" else "pos_v")
        if pos:
            self.move(*self._clamp_pos(pos[0], pos[1]))
        else:
            geo = QApplication.instance().primaryScreen().availableGeometry()
            self.move(geo.right() - self.width() - 20, geo.top() + 40)
        self._save_cfg()

    def _set_topmost(self, on):
        flags = self.windowFlags()
        flags = (flags | Qt.WindowStaysOnTopHint) if on \
            else (flags & ~Qt.WindowStaysOnTopHint)
        self.setWindowFlags(flags)
        self.show()

    def _store_pos(self, layout_key):
        g = self.frameGeometry().topLeft()
        self._cfg["pos_h" if layout_key == "h" else "pos_v"] = [g.x(), g.y()]

    def _clamp_pos(self, x, y):
        geo = QApplication.instance().primaryScreen().availableGeometry()
        x = max(geo.left(), min(int(x), geo.right() - self.width()))
        y = max(geo.top(), min(int(y), geo.bottom() - self.height()))
        return x, y

    def _save_cfg(self):
        self._store_pos(self._layout_key)
        self._cfg["scale"] = round(self._scale, 3)
        self._cfg["layout"] = self._layout_key
        self._cfg["theme"] = self._theme_key
        self._cfg["opacity"] = round(self.windowOpacity(), 2)
        self._cfg["topmost"] = bool(
            self.windowFlags() & Qt.WindowStaysOnTopHint)
        save_config(self._cfg)

    # --- 数据更新 ----------------------------------------------------------

    def _poll_data(self):
        d = self.worker.poll()
        if d is not None:
            self._on_data(d)

    def _on_data(self, d: dict):
        self.cluster.set_value("cpu_load", d.get("cpu_load"))
        self.cluster.set_value("gpu_load", d.get("gpu_load"))
        self.cluster.set_value("npu_load", d.get("npu_load"))
        self.cluster.set_value("cpu_temp", d.get("cpu_temp"))
        self.cluster.set_value("npu_gpu_temp", d.get("gpu_temp"))
        self.cluster.set_extra(d.get("mem_used_gb"), d.get("mem_load"),
                               d.get("disk_c_rate"), d.get("disk_d_rate"),
                               d.get("wifi_pct"), d.get("battery_pct"))
        t = d.get("cpu_temp")
        if t is not None and (self._last_temp is None
                              or abs(t - self._last_temp) >= 0.5):
            self._last_temp = t
            self.tray.setIcon(make_temp_icon(t, self._th))
            self.tray.setToolTip(
                f"CPU {d.get('cpu_load') or 0:.0f}% {t:.0f}°C | "
                f"GPU {d.get('gpu_load') or 0:.0f}% | "
                f"NPU {d.get('npu_load') or 0:.0f}% | "
                f"内存 {d.get('mem_load') or 0:.0f}%")

    # --- 交互 --------------------------------------------------------------

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_pos = (ev.globalPosition().toPoint()
                              - self.frameGeometry().topLeft())

    def mouseMoveEvent(self, ev):
        if self._drag_pos is not None and ev.buttons() == Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.RightButton:
            # 在「右键松开」时弹菜单，而不是按下时或等系统的 WM_CONTEXTMENU：
            #  - 松开时弹，松开事件已被本窗口消费，不会落到菜单上误触发菜单项
            #  - 不依赖 WM_CONTEXTMENU 的派发时机（本机会延迟近 2 秒才派发）
            #  - 配合 popup() 使用，避免 exec() 的嵌套事件循环把菜单当场关闭
            self._show_context_menu(ev.globalPosition().toPoint())
            return
        self._drag_pos = None
        if ev.button() == Qt.LeftButton:
            self._save_cfg()   # 拖拽结束记住位置

    def wheelEvent(self, ev):
        # 悬停时 Ctrl+滚轮 无级缩放
        if ev.modifiers() & Qt.ControlModifier:
            step = SCALE_STEP if ev.angleDelta().y() > 0 else -SCALE_STEP
            self.set_scale(self._scale + step)
            ev.accept()
        else:
            ev.ignore()

    def _show_context_menu(self, pos):
        # 复用的旧菜单先回收（父对象为 self，否则子对象会一直堆积）
        if self._menu is not None:
            self._menu.deleteLater()
            self._menu = None
        menu = QMenu(self)
        self._menu = menu
        a_hide = menu.addAction("最小化到托盘")
        a_top = menu.addAction("置顶")
        a_top.setCheckable(True)
        a_top.setChecked(bool(self.windowFlags() & Qt.WindowStaysOnTopHint))

        m_layout = menu.addMenu("布局")
        for key in ("h", "v"):
            act = m_layout.addAction(LAYOUTS[key]["label"])
            act.setCheckable(True)
            act.setChecked(key == self._layout_key)
            act.triggered.connect(lambda chk, k=key: self.set_layout(k))

        m_theme = menu.addMenu("主题")
        for key, th in THEMES.items():
            act = m_theme.addAction(th["label"])
            act.setCheckable(True)
            act.setChecked(key == self._theme_key)
            act.triggered.connect(lambda chk, k=key: self.set_theme(k))

        m_scale = menu.addMenu("缩放")
        for pct in (75, 100, 125, 150, 200):
            act = m_scale.addAction(f"{pct}%")
            act.setCheckable(True)
            act.setChecked(abs(self._scale * 100 - pct) < 1)
            act.triggered.connect(lambda chk, s=pct / 100: self.set_scale(s, True))
        hint = m_scale.addAction("悬停时 Ctrl+滚轮 微调")
        hint.setEnabled(False)

        menu.addSeparator()
        a_op50 = menu.addAction("透明度 50%")
        a_op80 = menu.addAction("透明度 80%")
        a_op100 = menu.addAction("透明度 100%")
        menu.addSeparator()
        a_auto = menu.addAction("开机自启")
        a_auto.setCheckable(True)
        a_auto.setChecked(is_autostart())
        menu.addSeparator()
        a_quit = menu.addAction("退出")

        a_hide.triggered.connect(self.hide_to_tray)
        a_top.triggered.connect(lambda checked: self._set_topmost(checked))
        a_op50.triggered.connect(lambda: self._set_opacity(0.5))
        a_op80.triggered.connect(lambda: self._set_opacity(0.8))
        a_op100.triggered.connect(lambda: self._set_opacity(1.0))
        a_auto.triggered.connect(lambda checked: set_autostart(checked))
        a_quit.triggered.connect(self._quit)

        # 必须用 popup() 而非 exec()：exec() 的嵌套事件循环会把随后到达的
        # WM_CONTEXTMENU 事件转交给菜单，菜单当场自关（打包后必现，菜单一闪就没）
        menu.popup(pos)

    def _set_opacity(self, v):
        self.setWindowOpacity(v)
        self._save_cfg()

    def _toggle_visible(self):
        self.setVisible(not self.isVisible())

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.setVisible(True)
            self.raise_()

    def hide_to_tray(self):
        self.hide()
        self._save_cfg()
        if self._first_hide:
            self._first_hide = False
            self.tray.showMessage(
                "Win11 Dash", "已最小化到系统托盘，双击托盘图标重新显示",
                QSystemTrayIcon.Information, 2500)

    def _quit(self):
        self._save_cfg()
        self.worker.stop()
        self.tray.hide()
        QApplication.instance().quit()

    # --- 背景 --------------------------------------------------------------

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(self._scale, self._scale)   # 与表盘同一逻辑坐标系
        th = self._th
        rect = QRectF(2, 2, LAYOUTS[self._layout_key]["w"] - 4,
                      LAYOUTS[self._layout_key]["h"] - 4)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g0, g1 = th["win_grad"]
        grad.setColorAt(0, C(g0))
        grad.setColorAt(1, C(g1))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(C(th["win_border"]), 1))
        p.drawRoundedRect(rect, 24, 24)
        p.end()

    def closeEvent(self, ev):
        ev.ignore()
        self.hide_to_tray()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    cfg = load_config()
    w = DashWindow(cfg)
    geo = app.primaryScreen().availableGeometry()
    pos = cfg.get("pos_h" if w._layout_key == "h" else "pos_v")
    if pos:
        x, y = w._clamp_pos(pos[0], pos[1])
    else:
        x, y = geo.right() - w.width() - 20, geo.top() + 40
    w.move(x, y)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
