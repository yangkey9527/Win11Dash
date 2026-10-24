# -*- coding: utf-8 -*-
"""
传感器数据采集模块
数据通道（按优先级）：
1. Core Temp 共享内存  -> CPU 温度/负载（免管理员，需 Core Temp 后台运行）
2. LibreHardwareMonitor -> GPU 占用（免管理员）；GPU 温度（需管理员）
3. psutil               -> 兜底占用率
所有 LHM 调用必须发生在同一线程（由 SensorWorker 保证）。
"""
import ctypes
from ctypes import wintypes
import os
import struct
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# PyInstaller 打包后 DLL 在 exe 同级 _internal/lib 或解压目录 lib
for cand in (os.path.join(BASE_DIR, "lib"),
             os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "lib")):
    if os.path.isdir(cand) and cand not in sys.path:
        sys.path.insert(0, cand)

# ---------------------------------------------------------------------------
# Core Temp 共享内存读取器
# ---------------------------------------------------------------------------

_k32 = ctypes.windll.kernel32
_k32.OpenFileMappingW.restype = ctypes.c_void_p
_k32.OpenFileMappingW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
_k32.MapViewOfFile.restype = ctypes.c_void_p
_k32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                               ctypes.c_ulong, ctypes.c_ulong, ctypes.c_size_t]
_k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

FILE_MAP_READ = 0x0004
CT_MAP_NAME = "CoreTempMappingObjectEx"
CT_SIZE = 4096  # 覆盖旧版结构全部字段（fTemp/uiLoad/uiCoreCnt/标志位/uiStructVersion）


class CoreTempReader:
    """读取 Core Temp 共享内存（只读，不创建）"""

    def __init__(self):
        self._handle = None

    def _ensure_open(self) -> bool:
        if self._handle:
            return True
        h = _k32.OpenFileMappingW(FILE_MAP_READ, False, CT_MAP_NAME)
        if h:
            self._handle = h
            return True
        return False

    def available(self) -> bool:
        return self._ensure_open()

    def read(self):
        """返回 {cpu_temp, cpu_load, per_core_temps} 或 None"""
        if not self._ensure_open():
            return None
        p = _k32.MapViewOfFile(self._handle, FILE_MAP_READ, 0, 0, CT_SIZE)
        if not p:
            self.close()
            return None
        try:
            buf = ctypes.string_at(p, CT_SIZE)
        except OSError:
            self.close()
            return None
        finally:
            _k32.UnmapViewOfFile(p)

        ui_core_cnt = struct.unpack_from("<I", buf, 1536)[0]
        if not (0 < ui_core_cnt <= 128):
            return None
        f_temps = struct.unpack_from("<256f", buf, 1544)
        ui_loads = struct.unpack_from("<256I", buf, 0)
        uc_fahrenheit = buf[2684]
        uc_delta = buf[2685]

        temps = list(f_temps[:ui_core_cnt])
        loads = list(ui_loads[:ui_core_cnt])

        # 数据 sanity check
        if not any(0 < t < 150 for t in temps):
            return None

        if uc_delta:  # 距离 TjMax 的差值 -> 还原绝对温度
            tj = struct.unpack_from("<128I", buf, 1024)[0] or 100
            temps = [tj - t for t in temps]
        if uc_fahrenheit:  # 华氏 -> 摄氏
            temps = [(t - 32) * 5 / 9 for t in temps]

        return {
            "cpu_temp": max(temps),
            "cpu_load": sum(loads) / len(loads) if loads else None,
            "per_core_temps": temps,
        }

    def close(self):
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# NPU 占用读取器（PDH GPU Engine 过滤 engtype_Neural，无需管理员）
# ---------------------------------------------------------------------------

_pdh = ctypes.windll.pdh

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = -2147481646  # 0x800007D2


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD),
                ("_pad", wintypes.DWORD),
                ("doubleValue", ctypes.c_double)]


class _PDH_FMT_COUNTERVALUE_ITEM(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR),
                ("FmtValue", _PDH_FMT_COUNTERVALUE)]


class NpuReader:
    """Intel NPU 占用率（\\GPU Engine(*)\\Utilization Percentage
    中 engtype_Neural 实例求和）"""

    def __init__(self):
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        st = _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
        if st != 0:
            self._query = None
            return
        st = _pdh.PdhAddCounterW(
            self._query,
            "\\GPU Engine(*)\\Utilization Percentage",
            0, ctypes.byref(self._counter))
        if st != 0:
            _pdh.PdhCloseQuery(self._query)
            self._query = None
            return
        _pdh.PdhCollectQueryData(self._query)  # 首次采集初始化计数器

    def available(self) -> bool:
        return self._query is not None

    def read(self):
        """返回 NPU 占用率（0~100），无 NPU 引擎时返回 0"""
        if not self._query:
            return None
        if _pdh.PdhCollectQueryData(self._query) != 0:
            return None
        buf_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        st = _pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, ctypes.byref(buf_size),
            ctypes.byref(item_count), None)
        if st != PDH_MORE_DATA or buf_size.value == 0:
            return None
        buf = ctypes.create_string_buffer(buf_size.value)
        st = _pdh.PdhGetFormattedCounterArrayW(
            self._counter, PDH_FMT_DOUBLE, ctypes.byref(buf_size),
            ctypes.byref(item_count), buf)
        if st != 0:
            return None
        total = 0.0
        found = False
        item_size = ctypes.sizeof(_PDH_FMT_COUNTERVALUE_ITEM)
        for i in range(item_count.value):
            item = _PDH_FMT_COUNTERVALUE_ITEM.from_buffer_copy(
                buf.raw[i * item_size:(i + 1) * item_size])
            if item.szName and "engtype_Neural" in item.szName:
                if item.FmtValue.CStatus == 0:
                    total += item.FmtValue.doubleValue
                    found = True
        return min(total, 100.0) if found else None

    def close(self):
        if self._query:
            _pdh.PdhCloseQuery(self._query)
            self._query = None


# ---------------------------------------------------------------------------
# 磁盘速率读取器（PDH LogicalDisk 读+写 字节/秒，免管理员）
# ---------------------------------------------------------------------------

class DiskIoReader:
    """按盘符返回实时传输速率（读+写，MB/s）"""

    def __init__(self, letters=("C", "D")):
        self._query = ctypes.c_void_p()
        self._counters = {}  # 盘符 -> (读计数器, 写计数器)
        st = _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
        if st != 0:
            self._query = None
            return
        for L in letters:
            pair = []
            for suffix in ("\\Disk Read Bytes/sec", "\\Disk Write Bytes/sec"):
                c = ctypes.c_void_p()
                # 英文计数器名（本地化系统也可用）
                st = _pdh.PdhAddEnglishCounterW(
                    self._query, f"\\LogicalDisk({L}:){suffix}",
                    0, ctypes.byref(c))
                if st != 0:
                    pair = None
                    break
                pair.append(c)
            if pair:
                self._counters[L] = pair
        if not self._counters:
            _pdh.PdhCloseQuery(self._query)
            self._query = None
            return
        _pdh.PdhCollectQueryData(self._query)  # 首次采集建立基准

    def available(self):
        return self._query is not None

    def read(self):
        """返回 {盘符: MB/s}（速率为两次采集间隔均值）"""
        if not self._query:
            return {}
        if _pdh.PdhCollectQueryData(self._query) != 0:
            return {}
        out = {}
        val = _PDH_FMT_COUNTERVALUE()
        for L, counters in self._counters.items():
            tot = 0.0
            for c in counters:
                if _pdh.PdhGetFormattedCounterValue(
                        c, PDH_FMT_DOUBLE, None, ctypes.byref(val)) == 0 \
                        and val.CStatus == 0:
                    tot += val.doubleValue
            out[L] = tot / (1024 * 1024)
        return out

    def close(self):
        if self._query:
            _pdh.PdhCloseQuery(self._query)
            self._query = None


# ---------------------------------------------------------------------------
# WiFi 信号读取器（Native WiFi API，免管理员、无子进程，frozen 环境可靠）
# ---------------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("Guid", _GUID),
                ("strInterfaceDescription", wintypes.WCHAR * 256),
                ("isState", wintypes.DWORD)]


class _WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wintypes.DWORD),
                ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", _WLAN_INTERFACE_INFO * 1)]


class _DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", wintypes.DWORD),
                ("ucSSID", ctypes.c_ubyte * 32)]


class _WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("dot11Ssid", _DOT11_SSID),
                ("dot11BssType", wintypes.DWORD),
                ("dot11Bssid", ctypes.c_ubyte * 6),
                ("dot11PhyType", wintypes.DWORD),
                ("uDot11PhyIndex", wintypes.DWORD),
                ("wlanSignalQuality", wintypes.DWORD),
                ("ulRxRate", wintypes.DWORD),
                ("ulTxRate", wintypes.DWORD)]


class _WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("isState", wintypes.DWORD),
                ("wlanConnectionMode", wintypes.DWORD),
                ("strProfileName", wintypes.WCHAR * 256),
                ("wlanAssociationAttributes", _WLAN_ASSOCIATION_ATTRIBUTES),
                ("bSecurityEnabled", wintypes.BOOL),
                ("bOneXEnabled", wintypes.BOOL),
                ("dot11AuthAlgorithm", wintypes.DWORD),
                ("dot11CipherAlgorithm", wintypes.DWORD)]


class WifiReader:
    """wlanapi.dll 直读当前连接信号质量（0~100）"""

    _OPCODE_CURRENT_CONNECTION = 7

    def __init__(self):
        self._ok = False
        self._guid = None
        try:
            self._api = ctypes.windll.wlanapi
        except OSError:
            return
        vp = ctypes.POINTER(ctypes.c_void_p)
        self._api.WlanOpenHandle.argtypes = [
            wintypes.DWORD, ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD), vp]
        self._api.WlanEnumInterfaces.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, vp]
        self._api.WlanQueryInterface.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_GUID), wintypes.DWORD,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), vp,
            ctypes.c_void_p]
        self._api.WlanFreeMemory.argtypes = [ctypes.c_void_p]
        self._api.WlanCloseHandle.argtypes = [ctypes.c_void_p,
                                              ctypes.c_void_p]
        self._handle = ctypes.c_void_p()
        ver = wintypes.DWORD(0)
        if self._api.WlanOpenHandle(2, None, ctypes.byref(ver),
                                    ctypes.byref(self._handle)) != 0:
            return
        self._ok = self._refresh_guid()

    def _refresh_guid(self):
        plist = ctypes.c_void_p()
        if self._api.WlanEnumInterfaces(self._handle, None,
                                        ctypes.byref(plist)) != 0 \
                or not plist.value:
            return False
        try:
            lst = ctypes.cast(
                plist, ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)).contents
            if lst.dwNumberOfItems < 1:
                return False
            self._guid = lst.InterfaceInfo[0].Guid
            return True
        finally:
            self._api.WlanFreeMemory(plist)

    def available(self):
        return self._ok

    def read(self):
        """返回信号强度 0~100；未连接/失败返回 None"""
        if not self._ok or self._guid is None:
            return None
        for attempt in range(2):
            size = wintypes.DWORD(0)
            pdata = ctypes.c_void_p()
            st = self._api.WlanQueryInterface(
                self._handle, ctypes.byref(self._guid),
                self._OPCODE_CURRENT_CONNECTION, None,
                ctypes.byref(size), ctypes.byref(pdata), None)
            if st == 0 and pdata.value:
                try:
                    attr = ctypes.cast(
                        pdata,
                        ctypes.POINTER(_WLAN_CONNECTION_ATTRIBUTES)).contents
                    if attr.isState != 1:  # 1 = connected
                        return None
                    q = attr.wlanAssociationAttributes.wlanSignalQuality
                    return max(0, min(100, int(q)))
                finally:
                    self._api.WlanFreeMemory(pdata)
            if attempt == 0:
                self._refresh_guid()  # 网卡可能变化，重枚举一次
        return None

    def close(self):
        if self._ok:
            self._api.WlanCloseHandle(self._handle, None)
            self._ok = False


# ---------------------------------------------------------------------------
# LibreHardwareMonitor 读取器
# ---------------------------------------------------------------------------

class LhmReader:
    """LibreHardwareMonitor 读取器（必须在同一线程创建与调用）"""

    def __init__(self):
        import clr  # pythonnet
        clr.AddReference("LibreHardwareMonitorLib")
        from LibreHardwareMonitor.Hardware import Computer
        import LibreHardwareMonitor.Hardware as HW
        self._SensorType = HW.SensorType

        self.computer = Computer()
        self.computer.IsCpuEnabled = True
        self.computer.IsGpuEnabled = True
        self.computer.IsMemoryEnabled = True
        self.computer.IsMotherboardEnabled = True  # 风扇在主板 SuperIO
        self.computer.Open()

        self._cpu_hw = None
        self._gpu_hw = None
        self._mem_hw = None
        for hw in self.computer.Hardware:
            t = str(hw.HardwareType)
            if t == "Cpu":
                self._cpu_hw = hw
            elif t.startswith("Gpu"):
                self._gpu_hw = hw
            elif t == "Memory" and self._mem_hw is None:
                self._mem_hw = hw
        self._update_all()  # 预热

        # MSR 直读温度（PawnIO 驱动，Core Temp 同款原理）
        self._msr = None
        self._tjmax = None
        try:
            from LibreHardwareMonitor.PawnIo import IntelMsr
            msr = IntelMsr()
            ok, val = msr.ReadMsr(0x1A2, 0)
            if ok:
                tj = (val >> 16) & 0xFF
                if 50 < tj < 130:
                    self._msr = msr
                    self._tjmax = tj
                else:
                    msr.Close()
            else:
                msr.Close()
        except Exception as e:
            print(f"[sensor] MSR 初始化失败: {e}")

    def _msr_temp(self):
        """MSR 0x19C: bit31 valid, bits22:16 = TjMax - 温度"""
        if self._msr is None:
            return None
        try:
            ok, val = self._msr.ReadMsr(0x19C, 0)
            if ok and ((val >> 31) & 1):
                delta = (val >> 16) & 0x7F
                t = self._tjmax - delta
                if 0 < t < 150:
                    return float(t)
        except Exception:
            pass
        return None

    def _update_all(self):
        for hw in self.computer.Hardware:
            hw.Update()
            for sub in hw.SubHardware:
                sub.Update()

    def _pick(self, hw, sensor_type, names):
        if hw is None:
            return None
        found = {}
        for s in hw.Sensors:
            if s.SensorType == sensor_type and s.Value is not None:
                found[str(s.Name)] = float(s.Value)
        for n in names:
            if n in found:
                return found[n]
        return None

    def _gpu_engine_max(self):
        if self._gpu_hw is None:
            return None
        st = self._SensorType.Load
        vals = [float(s.Value) for s in self._gpu_hw.Sensors
                if s.SensorType == st and s.Value is not None
                and str(s.Name).startswith("D3D")]
        return max(vals) if vals else None

    def _cpu_clock_avg(self):
        """CPU 核心频率平均（MHz）"""
        if self._cpu_hw is None:
            return None
        st = self._SensorType.Clock
        vals = [float(s.Value) for s in self._cpu_hw.Sensors
                if s.SensorType == st and s.Value is not None
                and str(s.Name).startswith("CPU Core #")]
        return sum(vals) / len(vals) if vals else None

    def _fan_rpm(self):
        """遍历所有硬件（含主板子硬件）找风扇转速"""
        st = self._SensorType.Fan

        def scan(hw):
            best = None
            for s in hw.Sensors:
                if s.SensorType == st and s.Value is not None:
                    n = str(s.Name)
                    if best is None or "cpu" in n.lower():
                        best = float(s.Value)
                        if "cpu" in n.lower():
                            return best
            for sub in hw.SubHardware:
                v = scan(sub)
                if v is not None:
                    return v
            return best

        for hw in self.computer.Hardware:
            v = scan(hw)
            if v is not None:
                return v
        return None

    def read(self):
        self._update_all()
        st = self._SensorType
        data = {
            "cpu_load": self._pick(self._cpu_hw, st.Load, ["CPU Total"]),
            "cpu_temp": self._pick(self._cpu_hw, st.Temperature,
                                   ["CPU Package", "Core Average", "Core Max",
                                    "CPU Core Max", "CPU Core"])
                    or self._msr_temp(),
            "gpu_load": self._gpu_engine_max(),
            "gpu_temp": self._pick(self._gpu_hw, st.Temperature,
                                   ["GPU Core", "GPU Hot Spot", "GPU Memory"]),
            "mem_load": self._pick(self._mem_hw, st.Load, ["Memory"]),
            "cpu_clock": self._cpu_clock_avg(),
            "fan_rpm": self._fan_rpm(),
        }
        if data["gpu_load"] is None:
            data["gpu_load"] = self._pick(self._gpu_hw, st.Load,
                                          ["GPU Core", "GPU"])
        return data

    def close(self):
        try:
            if self._msr is not None:
                self._msr.Close()
        except Exception:
            pass
        try:
            self.computer.Close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# psutil 兜底
# ---------------------------------------------------------------------------

class PsutilReader:
    def __init__(self):
        import psutil
        self._psutil = psutil
        psutil.cpu_percent(None)

    def read(self):
        freq = self._psutil.cpu_freq()
        return {
            "cpu_load": self._psutil.cpu_percent(None),
            "cpu_temp": None,
            "gpu_load": None,
            "gpu_temp": None,
            "mem_load": self._psutil.virtual_memory().percent,
            "cpu_clock": freq.current if freq else None,
            "fan_rpm": None,
        }

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 组合读取器
# ---------------------------------------------------------------------------

class HybridReader:
    """LHM（占用/温度） + NPU(PDH) + Core Temp/psutil 补位 组合"""

    def __init__(self):
        self.coretemp = CoreTempReader()
        try:
            self.lhm = LhmReader()
        except Exception as e:
            print(f"[sensor] LHM 初始化失败: {e}")
            self.lhm = None
        try:
            self.npu = NpuReader()
        except Exception as e:
            print(f"[sensor] NPU 初始化失败: {e}")
            self.npu = None
        try:
            self.wifi = WifiReader()
        except Exception as e:
            print(f"[sensor] WiFi 初始化失败: {e}")
            self.wifi = None
        try:
            self.diskio = DiskIoReader()
        except Exception as e:
            print(f"[sensor] DiskIo 初始化失败: {e}")
            self.diskio = None
        self._psutil_fallback = None
        self._slow_tick = 0
        self._slow = {"disk_c_rate": None, "disk_d_rate": None,
                      "wifi_pct": None, "battery_pct": None}
        print(f"[sensor] LHM={'OK' if self.lhm else 'N/A'} "
              f"NPU={'OK' if self.npu and self.npu.available() else 'N/A'} "
              f"WiFi={'OK' if self.wifi and self.wifi.available() else 'N/A'}")

    def _read_slow(self):
        """慢速数据：磁盘速率 / WiFi 信号 / 电池电量（变化慢，低频刷新）"""
        info = {}
        if self.diskio:
            rates = self.diskio.read()
            if "C" in rates:
                info["disk_c_rate"] = round(rates["C"], 1)
            if "D" in rates:
                info["disk_d_rate"] = round(rates["D"], 1)
        try:
            import psutil as pu
            b = pu.sensors_battery()
            if b is not None:
                info["battery_pct"] = round(b.percent)
        except Exception:
            pass
        wifi = self.wifi.read() if self.wifi else None
        if wifi is None:  # 兜底：netsh 解析
            try:
                import subprocess
                r = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                   capture_output=True, text=True, timeout=3,
                                   creationflags=getattr(
                                       subprocess, "CREATE_NO_WINDOW", 0))
                for line in r.stdout.splitlines():
                    if ("信号" in line or "Signal" in line) and "%" in line:
                        wifi = int(line.split(":")[-1].strip().rstrip("%"))
                        break
            except Exception:
                pass
        info["wifi_pct"] = wifi
        return info

    def read(self):
        out = {"cpu_load": None, "cpu_temp": None,
               "gpu_load": None, "gpu_temp": None, "mem_load": None,
               "cpu_clock": None, "mem_used_gb": None, "npu_load": None}

        # LHM 主通道（管理员下含温度；非管理员有占用/频率）
        if self.lhm:
            try:
                d = self.lhm.read()
                out["gpu_load"] = d["gpu_load"]
                out["gpu_temp"] = d["gpu_temp"]
                out["cpu_clock"] = d.get("cpu_clock")
                out["cpu_load"] = d["cpu_load"]
                out["cpu_temp"] = d["cpu_temp"]
                out["mem_load"] = d["mem_load"]
            except Exception as e:
                print(f"[sensor] LHM 读取失败: {e}")

        # NPU 占用（PDH，免管理员）
        if self.npu:
            try:
                out["npu_load"] = self.npu.read()
            except Exception as e:
                print(f"[sensor] NPU 读取失败: {e}")

        # Core Temp 补位（非管理员 + Core Temp 运行时的温度来源）
        if out["cpu_temp"] is None or out["cpu_load"] is None:
            ct = self.coretemp.read()
            if ct:
                if out["cpu_temp"] is None:
                    out["cpu_temp"] = ct["cpu_temp"]
                if out["cpu_load"] is None:
                    out["cpu_load"] = ct["cpu_load"]

        # 集显与 CPU 同 die：无独立 GPU 温度时复用 CPU 最高温
        if out["gpu_temp"] is None:
            out["gpu_temp"] = out["cpu_temp"]

        # psutil 补位：频率 / 占用 / 内存
        if self._psutil_fallback is None:
            self._psutil_fallback = PsutilReader()
        pd = self._psutil_fallback.read()
        if out["cpu_load"] is None:
            out["cpu_load"] = pd["cpu_load"]
        if out["mem_load"] is None:
            out["mem_load"] = pd["mem_load"]
        if out["cpu_clock"] is None:
            out["cpu_clock"] = pd["cpu_clock"]
        out["mem_used_gb"] = round(
            self._psutil_fallback._psutil.virtual_memory().used
            / (1024 ** 3), 1)

        # 慢速数据：每 5 次刷新一次
        self._slow_tick += 1
        if self._slow_tick % 5 == 1 or self._slow["disk_c_rate"] is None:
            self._slow.update(self._read_slow())
        out.update(self._slow)

        return out

    def close(self):
        self.coretemp.close()
        if self.npu:
            self.npu.close()
        if self.wifi:
            self.wifi.close()
        if self.diskio:
            self.diskio.close()
        if self.lhm:
            self.lhm.close()


def create_reader():
    return HybridReader()
