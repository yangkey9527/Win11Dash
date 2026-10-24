# -*- coding: utf-8 -*-
"""
Win11 Dash — 五连表桌面悬浮仪表盘（帕拉梅拉摆放，发光弧线风格）
左→右: CPU温度(小,开口朝右) | GPU占用(中,开口朝右) | CPU占用(大,内嵌内存血槽)
       | NPU占用(中,开口朝左) | GPU温度(小,开口朝左)
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

from sensor import create_reader


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def temp_color(t) -> QColor:
    if t is None:
        return QColor(255, 255, 255, 120)
    if t < 60:
        return QColor(0, 242, 234)
    if t < 75:
        return QColor(255, 200, 0)
    if t < 90:
        return QColor(255, 107, 0)
    return QColor(255, 45, 85)


def make_temp_icon(temp) -> QIcon:
    """托盘温度图标（黑底圆角方块 + 温度数字）"""
    S = 64
    pm = QPixmap(S, S)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(13, 17, 23, 240))
    p.setPen(QPen(QColor(48, 54, 61), 2))
    p.drawRoundedRect(1, 1, S - 2, S - 2, 12, 12)
    txt = "--" if temp is None else f"{temp:.0f}"
    col = temp_color(temp)
    fs = 34 if len(txt) <= 2 else 26
    p.setFont(QFont("Segoe UI", fs, QFont.Weight.Bold))
    p.setPen(col)
    p.drawText(QRectF(0, -4, S, S), Qt.AlignCenter, txt)
    p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    p.setPen(QColor(255, 255, 255, 140))
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
# 表盘规格：发光弧线风格
# (key, cx, cy, r, vmin, vmax, color_a, color_b, label, unit, a0, span)
#   a0   = 0 值所在数学角（度，0=正右，逆时针为正）
#   span = 满量程扫掠角（带符号：负=顺时针，正=逆时针）
# 左侧两表开口朝右（缺口藏入右邻遮挡区），右侧两表开口朝左，中表标准。
# ---------------------------------------------------------------------------

GAUGES = [
    ("cpu_temp", 92, 133, 56, 30, 110,
     QColor(0, 242, 234), QColor(255, 45, 85), "CPU 温度", "°C",
     285, -210),
    ("gpu_load", 192, 120, 70, 0, 100,
     QColor(168, 85, 247), QColor(255, 45, 85), "GPU 占用", "%",
     285, -210),
    ("cpu_load", 310, 105, 92, 0, 100,
     QColor(0, 242, 234), QColor(0, 120, 255), "CPU 占用", "%",
     225, -270),
    ("npu_load", 428, 120, 70, 0, 100,
     QColor(0, 220, 255), QColor(150, 110, 255), "NPU 占用", "%",
     255, 210),
    ("npu_gpu_temp", 528, 133, 56, 30, 110,
     QColor(0, 242, 234), QColor(255, 45, 85), "GPU 温度", "°C",
     255, 210),
]

WIN_W, WIN_H = 620, 210


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


def is_autostart():
    r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                       capture_output=True)
    return r.returncode == 0


def set_autostart(on):
    if on:
        subprocess.run(["schtasks", "/create", "/tn", TASK_NAME,
                        "/tr", _autostart_cmd(), "/sc", "onlogon",
                        "/rl", "highest", "/f"], capture_output=True)
    else:
        subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                       capture_output=True)

def arc_rect(cx, cy, r):
    return QRectF(cx - r, cy - r, r * 2, r * 2)


# ---------------------------------------------------------------------------
# 五连表控件
# ---------------------------------------------------------------------------

class ClusterWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cur = {k: None for k, *_ in GAUGES}   # None = 尚无数据
        self._tgt = {k: 0.0 for k, *_ in GAUGES}
        self.extra = {"mem_used_gb": None, "mem_load": None,
                      "disk_c_rate": None, "disk_d_rate": None,
                      "wifi_pct": None, "battery_pct": None}
        # 磁盘速率自适应峰值（血槽比例尺，缓慢衰减）
        self._disk_peak = {"C": 50.0, "D": 50.0}
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(30)

    def set_value(self, key, v):
        if v is not None and key in self._tgt:
            v = float(v)
            if math.isnan(v):
                return
            # 钳到量程内（LHM 传感器值偶发超界）
            for g in GAUGES:
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
        # 先画外侧小表，后画大表（交叠层次）
        for idx in (0, 4, 1, 3, 2):
            key, cx, cy, r, vmin, vmax, ca, cb, label, unit, a0, span \
                = GAUGES[idx]
            cur = self._cur[key]
            ratio = 0.0 if cur is None else max(
                0.0, min(1.0, (cur - vmin) / (vmax - vmin)))
            self._draw_gauge(p, cx, cy, r, vmin, vmax, ca, cb, label, unit,
                             a0, span, ratio, cur)
            if key == "cpu_load":
                self._draw_center_digits(p, cx, cy, r)
        self._draw_side_widgets(p)
        p.end()

    def _mini_bar(self, p, x, y, w, h, ratio, grad=None, color=None):
        """小横槽：x,y 为中心线起点，ratio 0~1"""
        by = y - h / 2
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(QColor(255, 255, 255, 18))
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
                g.setColorAt(t, c)
            p.setBrush(QBrush(g))
        else:
            p.setBrush(color or QColor(0, 230, 118))
        p.drawRoundedRect(QRectF(x, by, fw, h), h / 2, h / 2)

    def _seg_bar(self, p, x, y, w, h, ratio, color):
        """方块分格槽：x,y 为中心线起点，点亮的格数随 ratio 增加"""
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
                p.setPen(QPen(QColor(255, 255, 255, 40), 1))
                p.setBrush(QColor(255, 255, 255, 18))
            p.drawRoundedRect(QRectF(sx, by, seg_w, h), 2, 2)

    def _draw_side_widgets(self, p):
        """左上：C:/D: 磁盘速率横槽（C 长 D 短，贴合下方表盘留白）；
        右上：WiFi / 电量横槽（与左侧对称，小图标作标签，数字在右）"""
        disk_grad = [(0.0, QColor(0, 242, 234)),
                     (0.6, QColor(255, 200, 0)),
                     (1.0, QColor(255, 45, 85))]
        # --- 左上：磁盘速率行（数字紧跟槽后；D 行下移贴合表盘留白） ---
        rows = [("C:", self.extra.get("disk_c_rate"), 100),
                ("D:", self.extra.get("disk_d_rate"), 60)]
        y = 23
        for label, val, bw in rows:
            if val is None and label == "D:":
                continue  # 无 D 盘跳过
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(QRectF(28, y - 8, 18, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, label)
            ratio = None
            if val is not None:
                ratio = val / self._disk_peak[label[0]]
            self._mini_bar(p, 46, y, bw, 7, ratio, grad=disk_grad)
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.setPen(QColor(240, 244, 248))
            p.drawText(QRectF(46 + bw + 5, y - 8, 58, 16),
                       Qt.AlignLeft | Qt.AlignVCenter, fmt_rate(val))
            y += 25

        # --- 右上：WiFi（长槽）/ 电量（短槽），与左侧镜像对称 ---
        # 布局（左->右）：数字(右对齐) | 分格槽 | 小图标
        wifi = self.extra.get("wifi_pct")
        bat = self.extra.get("battery_pct")
        on_c = QColor(0, 242, 234)
        off_c = QColor(255, 255, 255, 45)

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
        p.setPen(QColor(240, 244, 248))
        wtxt = "--" if wifi is None else f"{wifi}%"
        p.drawText(QRectF(411, 15, 56, 16),
                   Qt.AlignRight | Qt.AlignVCenter, wtxt)

        # 电量行（y=48，短槽 60）
        bat_col = None
        if bat is not None:
            bat_col = (QColor(255, 45, 85) if bat < 20
                       else QColor(255, 200, 0) if bat < 50
                       else QColor(0, 230, 118))
        self._seg_bar(p, 514, 48, 60, 7,
                      None if bat is None else bat / 100.0,
                      bat_col or QColor(0, 230, 118))
        # 电池小图标（含填充）
        bgx, bgy, bgw, bgh = 574, 43.5, 18, 9
        p.setPen(QPen(QColor(255, 255, 255, 90), 1.1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(bgx, bgy, bgw, bgh), 2.5, 2.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 90))
        p.drawRoundedRect(QRectF(bgx + bgw + 1, bgy + 2.5, 2.5, 4), 1, 1)
        if bat is not None:
            fw = (bgw - 3) * max(0.0, min(1.0, bat / 100.0))
            if fw > 0.5:
                p.setBrush(bat_col)
                p.drawRoundedRect(QRectF(bgx + 1.5, bgy + 1.5, fw, bgh - 3),
                                  1.5, 1.5)
        btxt = "--" if bat is None else f"{bat}%"
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        p.setPen(QColor(240, 244, 248))
        p.drawText(QRectF(452, 40, 55, 16),
                   Qt.AlignRight | Qt.AlignVCenter, btxt)

    def _draw_gauge(self, p, cx, cy, r, vmin, vmax, ca, cb, label, unit,
                    a0_deg, span_deg, ratio, raw):
        arc_r = r - 8
        pen_w = max(5.0, r * 0.11)
        start_q = int(a0_deg * 16)          # Qt 起始角（1/16 度）
        span_q = int(span_deg * 16)         # Qt 扫掠角（带符号）

        # 玻璃底
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 24, 32, 150))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r - 1, r - 1)

        # 刻度点（沿扇区均匀分布，暗白）
        n = 12
        for i in range(n + 1):
            a_deg = a0_deg + span_deg * i / n
            a = math.radians(a_deg)
            r_out = r - 2
            r_in = r - (7 if i % 3 == 0 else 5)
            p.setPen(QPen(QColor(255, 255, 255, 70 if i % 3 == 0 else 40),
                          1.4 if i % 3 == 0 else 1))
            p.drawLine(QPointF(cx + r_in * math.cos(a), cy - r_in * math.sin(a)),
                       QPointF(cx + r_out * math.cos(a), cy - r_out * math.sin(a)))

        # 背景弧（暗）
        pen = QPen(QColor(255, 255, 255, 22), pen_w, Qt.SolidLine,
                   Qt.RoundCap)
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

        # 中心读数（中央大表除外，数字区单独画）
        if r < 85:
            if unit == "°C":
                vtxt = "--" if raw is None else f"{raw:.0f}"
                vcol = temp_color(raw)
            else:
                vtxt = "--" if raw is None else f"{raw:.0f}"
                vcol = QColor(240, 240, 240)
            fs = max(11, int(r * 0.34))
            p.setFont(QFont("Segoe UI", fs, QFont.Weight.Bold))
            p.setPen(vcol)
            p.drawText(QRectF(cx - r, cy - r * 0.22, r * 2, r * 0.55),
                       Qt.AlignCenter, vtxt)
            p.setFont(QFont("Microsoft YaHei", max(7, int(r * 0.12)),
                            QFont.Weight.Bold))
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(QRectF(cx - r, cy + r * 0.36, r * 2, r * 0.22),
                       Qt.AlignCenter, f"{label} {unit}")
        # 中央大表不画标签（数字自带 %，保持盘面干净）

    def _draw_center_digits(self, p, cx, cy, r):
        """中央大表：CPU 占用大数字（右下角小 %） + 中下内存血槽"""
        load = self._cur["cpu_load"]
        load_txt = "--" if load is None else f"{load:.0f}"
        num_font = QFont("Segoe UI", 26, QFont.Weight.Bold)
        p.setFont(num_font)
        p.setPen(QColor(240, 244, 248))
        num_rect = QRectF(cx - 70, cy - r * 0.32, 140, 36)
        p.drawText(num_rect, Qt.AlignCenter, load_txt)
        # 小 % 紧跟大数字右下角
        w_num = p.fontMetrics().horizontalAdvance(load_txt)
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.setPen(QColor(255, 255, 255, 140))
        p.drawText(QRectF(cx + w_num / 2 + 3, num_rect.bottom() - 19, 18, 16),
                   Qt.AlignLeft | Qt.AlignBottom, "%")

        # 内存血槽（更窄更厚，下移嵌入表盘底部开口区；数字在血槽上方）
        mem_gb = self.extra["mem_used_gb"]
        mem_pct = self.extra.get("mem_load")
        bar_w, bar_h = r * 0.75, 9
        bx, by = cx - bar_w / 2, cy + r * 0.66
        # 数字行：血槽上方
        mem_txt = ("--" if mem_gb is None else f"{mem_gb:.1f}GB") \
            + ("  " if mem_pct is not None and mem_gb is not None else "") \
            + ("" if mem_pct is None else f"{mem_pct:.0f}%")
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.setPen(QColor(255, 220, 120))
        p.drawText(QRectF(bx - 15, by - 14, bar_w + 30, 12),
                   Qt.AlignCenter, mem_txt)
        # 槽底
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawRoundedRect(QRectF(bx, by, bar_w, bar_h), bar_h / 2,
                          bar_h / 2)
        # 填充
        if mem_pct is not None:
            ratio = max(0.0, min(1.0, mem_pct / 100.0))
            fw = bar_w * ratio
            if fw > 1:
                fg = QLinearGradient(bx, 0, bx + bar_w, 0)
                fg.setColorAt(0.0, QColor(0, 242, 234))
                fg.setColorAt(0.6, QColor(255, 200, 0))
                fg.setColorAt(1.0, QColor(255, 45, 85))
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
    def __init__(self):
        super().__init__()
        self._drag_pos = None
        self._last_temp = None
        self._first_hide = True
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(WIN_W, WIN_H)

        self.cluster = ClusterWidget(self)
        self.cluster.setGeometry(0, 0, WIN_W, WIN_H)

        from PySide6.QtWidgets import QPushButton
        self.btn_close = QPushButton("×", self)
        self.btn_close.setFixedSize(24, 24)
        self.btn_close.move(self.width() - 30, 6)
        self.btn_close.setStyleSheet(
            "QPushButton { color: rgba(255,255,255,120); background: transparent;"
            " border: none; font-size: 18px; font-weight: bold; }"
            "QPushButton:hover { color: #ff2d55; }")
        self.btn_close.clicked.connect(self.hide_to_tray)

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(make_temp_icon(None))
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

        self.worker = SensorWorker()
        self.worker.start()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_data)
        self._poll_timer.start(300)

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
            self.tray.setIcon(make_temp_icon(t))
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
        elif ev.button() == Qt.RightButton:
            self._show_context_menu(ev.globalPosition().toPoint())

    def mouseMoveEvent(self, ev):
        if self._drag_pos is not None and ev.buttons() == Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, ev):
        self._drag_pos = None

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        a_hide = menu.addAction("最小化到托盘")
        a_top = menu.addAction("置顶")
        a_top.setCheckable(True)
        a_top.setChecked(bool(self.windowFlags() & Qt.WindowStaysOnTopHint))
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
        chosen = menu.exec(pos)
        if chosen is a_top:
            self._toggle_topmost(a_top.isChecked())
        elif chosen is a_auto:
            set_autostart(a_auto.isChecked())
        elif chosen is a_op50:
            self.setWindowOpacity(0.5)
        elif chosen is a_op80:
            self.setWindowOpacity(0.8)
        elif chosen is a_op100:
            self.setWindowOpacity(1.0)
        elif chosen is a_hide:
            self.hide_to_tray()
        elif chosen is a_quit:
            self._quit()

    def _toggle_topmost(self, checked=True):
        flags = self.windowFlags()
        flags = (flags | Qt.WindowStaysOnTopHint) if checked \
            else (flags & ~Qt.WindowStaysOnTopHint)
        self.setWindowFlags(flags)
        self.show()

    def _toggle_visible(self):
        self.setVisible(not self.isVisible())

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.setVisible(True)
            self.raise_()

    def hide_to_tray(self):
        self.hide()
        if self._first_hide:
            self._first_hide = False
            self.tray.showMessage(
                "Win11 Dash", "已最小化到系统托盘，双击托盘图标重新显示",
                QSystemTrayIcon.Information, 2500)

    def _quit(self):
        self.worker.stop()
        self.tray.hide()
        QApplication.instance().quit()

    # --- 背景 --------------------------------------------------------------

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0, QColor(13, 17, 23, 200))
        grad.setColorAt(1, QColor(22, 27, 34, 200))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor(48, 54, 61, 160), 1))
        p.drawRoundedRect(rect, 24, 24)
        p.end()

    def closeEvent(self, ev):
        ev.ignore()
        self.hide_to_tray()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = DashWindow()
    geo = app.primaryScreen().availableGeometry()
    w.move(geo.right() - w.width() - 20, geo.top() + 40)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
