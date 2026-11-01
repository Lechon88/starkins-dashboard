import os, sys, time, platform, getpass, subprocess
import csv, ctypes, threading
from collections import deque
import psutil
from version import APP_VERSION
from updater import check_latest_release, download_latest_release, start_updated_app
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, pyqtSignal, QThread, QSettings
from PyQt6.QtGui import QPainter, QColor, QPen, QFont, QLinearGradient, QPainterPath, QBrush
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QFrame, QPushButton, QStackedWidget)

try:
    import pynvml
    pynvml.nvmlInit()
    GPU = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception:
    GPU = None

class UpdateCheckWorker(QThread):
    result = pyqtSignal(object)

    def run(self):
        try:
            self.result.emit({"release": check_latest_release()})
        except Exception as exc:
            self.result.emit({"error": str(exc)})


class UpdateDownloadWorker(QThread):
    result = pyqtSignal(object)

    def __init__(self, release):
        super().__init__()
        self.release = release

    def run(self):
        try:
            path = download_latest_release(self.release)
            self.result.emit({"path": str(path)})
        except Exception as exc:
            self.result.emit({"error": str(exc)})


class PresentMonWorker(QThread):
    """Read per-frame CSV from PresentMon without blocking the Qt event loop."""
    fps = pyqtSignal(int, object)
    status = pyqtSignal(str)

    def __init__(self, executable):
        super().__init__()
        self.executable = executable
        self._stop = threading.Event()
        self._process = None

    def stop(self):
        self._stop.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def run(self):
        if not self.executable or not os.path.isfile(self.executable):
            self.status.emit("FPS-Quelle nicht verfügbar")
            return
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._process = subprocess.Popen(
                [self.executable, "--output_stdout", "--no_console_stats", "--no_track_gpu"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8-sig", errors="replace", bufsize=1, creationflags=flags)
            self.status.emit("FPS-Messung bereit")
            reader = csv.reader(self._process.stdout)
            headers = None
            samples = {}
            last_emit = {}
            for row in reader:
                if self._stop.is_set():
                    break
                if not row:
                    continue
                if headers is None:
                    if "ProcessID" not in row:
                        continue
                    headers = [x.strip() for x in row]
                    continue
                try:
                    values = dict(zip(headers, row))
                    pid = int(values.get("ProcessID", "0"))
                    frame_ms = float(values.get("MsBetweenPresents") or values.get("MsBetweenDisplayChange") or 0)
                    if pid <= 0 or frame_ms <= 0:
                        continue
                    series = samples.setdefault(pid, deque(maxlen=90))
                    series.append(frame_ms)
                    now = time.monotonic()
                    if now - last_emit.get(pid, 0) >= 0.5:
                        self.fps.emit(pid, int(round(1000 / (sum(series) / len(series)))))
                        last_emit[pid] = now
                except (ValueError, TypeError, ZeroDivisionError):
                    continue
            if not self._stop.is_set():
                code = self._process.poll()
                self.status.emit("FPS nicht verfügbar (Windows-Berechtigung oder Capture)" if code else "FPS-Messung beendet")
        except Exception:
            self.status.emit("FPS nicht verfügbar")
        finally:
            if self._process and self._process.poll() is None:
                self._process.terminate()
            if self._process and self._process.stdout:
                self._process.stdout.close()


def presentmon_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "PresentMon.exe")


def foreground_pid():
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


class GamingOverlay(QWidget):
    """Compact always-on-top overlay; does not take keyboard focus from games."""
    close_requested = pyqtSignal()

    def __init__(self):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("Starkins Gaming Overlay")
        self.setFixedSize(430, 78)
        self._drag_offset = None
        self.settings = QSettings("Starkins", "Dashboard")
        self.setStyleSheet("""
            QWidget#overlay { background: rgba(12, 13, 18, 225); border: 1px solid #343643; border-radius: 14px; }
            QLabel { background: transparent; color: #f2f2f4; font-family: 'Segoe UI'; }
            QLabel#brand { color: #19d3a2; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
            QLabel#value { font-size: 17px; font-weight: 700; }
            QLabel#caption { color: #a3a1ac; font-size: 10px; }
            QLabel#fps { color: #19d3a2; font-size: 23px; font-weight: 800; }
            QPushButton#overlayClose { background: transparent; border: 0; color: #a3a1ac; padding: 0; font-size: 17px; }
            QPushButton#overlayClose:hover { color: #ffffff; background: #30313a; }
        """)
        self.setObjectName("overlay")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        root = QVBoxLayout(self); root.setContentsMargins(14, 8, 14, 8); root.setSpacing(3)
        head = QHBoxLayout(); head.setSpacing(8)
        brand = QLabel("STARKINS  ·  GAMING OVERLAY"); brand.setObjectName("brand")
        brand.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        head.addWidget(brand); head.addStretch(1)
        close_button = QPushButton("×"); close_button.setObjectName("overlayClose")
        close_button.setFixedSize(24, 22); close_button.setToolTip("Overlay schließen und Dashboard öffnen")
        close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_button.clicked.connect(self.close_requested.emit)
        head.addWidget(close_button)
        root.addLayout(head)
        row = QHBoxLayout(); row.setSpacing(16)
        self.labels = {}
        for key, caption in (("fps", "FPS"), ("cpu", "CPU"), ("gpu", "GPU"), ("ram", "RAM")):
            box = QVBoxLayout(); box.setSpacing(0)
            val = QLabel("–"); val.setObjectName("fps" if key == "fps" else "value")
            cap = QLabel(caption); cap.setObjectName("caption")
            val.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            cap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            box.addWidget(val); box.addWidget(cap); row.addLayout(box)
            self.labels[key] = val
        row.addStretch(1)
        root.addLayout(row)
        try:
            x, y = self.settings.value("overlay/x"), self.settings.value("overlay/y")
            if x is not None and y is not None:
                self.move(int(x), int(y))
        except (TypeError, ValueError):
            pass

    def update_metrics(self, metrics, fps):
        self.labels["fps"].setText(f"{fps}" if fps else "–")
        self.labels["cpu"].setText("–" if metrics.get("cpu") is None else f"{metrics['cpu']:.0f}%")
        self.labels["gpu"].setText("–" if metrics.get("gv") is None else f"{metrics['gv']:.0f}%")
        ram = metrics.get("vm")
        self.labels["ram"].setText("–" if ram is None else f"{ram.percent:.0f}%")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.settings.setValue("overlay/x", self.x())
        self.settings.setValue("overlay/y", self.y())


GREEN, CYAN, AMBER, RED, GREY, PURPLE = "#19d3a2", "#5ac8fa", "#f5a524", "#e5484d", "#6b6872", "#b784f5"
QSS = """
QWidget{background:#0b0b0d;color:#f2f2f4;font-family:'Segoe UI';}
QLabel{background:transparent;}
QFrame#card,QFrame#cardc{background:#131215;border:1px solid #232127;border-radius:18px;}
QFrame#cardc:hover{border:1px solid #5ac8fa;}
QFrame#side{background:#0f0e11;border:1px solid #1a191d;border-radius:18px;}
QLabel#h1{font-size:34px;font-weight:700;} QLabel#h2{font-size:20px;font-weight:600;}
QLabel#muted{color:#8a8790;font-size:12px;} QLabel#logo{font-size:22px;font-weight:800;letter-spacing:2px;}
QPushButton{background:#1a191d;border:1px solid #2a2830;border-radius:10px;padding:8px 14px;color:#f2f2f4;}
QPushButton:hover{background:#232127;border:1px solid #5ac8fa;}
QPushButton#updateButton{background:#12251f;border:1px solid #19d3a2;color:#fff;padding:8px 5px;}
QPushButton#updateButton:hover{background:#173d31;}
QPushButton#updateButton:disabled{color:#8a8790;}
"""
NAV_ON = "QLabel{background:#12251f;border:1px solid #19d3a2;border-radius:10px;padding:9px 12px;color:#fff;}"
NAV_OFF = ("QLabel{background:transparent;border:1px solid transparent;border-radius:10px;padding:9px 12px;color:#9a97a1;}"
           "QLabel:hover{background:#17161a;color:#fff;}")

def level(v):
    if v is None: return "Nicht verfügbar", GREY
    if v < 75: return "Optimal", GREEN
    if v < 90: return "Hoch", AMBER
    return "Kritisch", RED

def dot(color, text, sub=""):
    s = f"<br><span style='color:#8a8790;font-size:11px'>{sub}</span>" if sub else ""
    return f"<span style='color:{color}'>●</span>&nbsp; <b>{text}</b>{s}"

def speed(b):
    return f"{b/1048576:.1f} MB/s" if b >= 1048576 else f"{b/1024:.0f} KB/s"

def size(b):
    if b >= 2**30: return f"{b/2**30:.1f} GB"
    if b >= 2**20: return f"{b/2**20:.0f} MB"
    return f"{b/1024:.0f} KB"

def safe(fn, default=0):
    try: return fn()
    except psutil.Error: return default

def cpu_name():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except Exception:
        return platform.processor() or "CPU"

def gpu_name():
    try:
        n = pynvml.nvmlDeviceGetName(GPU)
        return n.decode() if isinstance(n, bytes) else n
    except Exception:
        pass
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_VideoController | Select -First 1).Name"],
            capture_output=True, text=True, timeout=6, creationflags=0x08000000).stdout.strip()
        return out or "Keine GPU erkannt"
    except Exception:
        return "Keine GPU erkannt"

class Card(QFrame):
    clicked = pyqtSignal()
    def __init__(self, click=False):
        super().__init__(); self.setObjectName("cardc" if click else "card")
        if click: self.setCursor(Qt.CursorShape.PointingHandCursor)
    def mousePressEvent(self, e):
        if self.objectName() == "cardc" and e.button() == Qt.MouseButton.LeftButton: self.clicked.emit()

class Nav(QLabel):
    clicked = pyqtSignal()
    def __init__(self, text):
        super().__init__(text); self.setCursor(Qt.CursorShape.PointingHandCursor); self.on(False)
    def on(self, a): self.setStyleSheet(NAV_ON if a else NAV_OFF)
    def mousePressEvent(self, e): self.clicked.emit()

class Gauge(QWidget):
    def __init__(self):
        super().__init__(); self.setFixedSize(112, 112); self.value = None; self.sub = ""
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(11, 11, 90, 90)
        pen = QPen(QColor("#26242a"), 9); pen.setCapStyle(Qt.PenCapStyle.RoundCap); p.setPen(pen)
        p.drawArc(r, 225 * 16, -270 * 16)
        if self.value:
            pen.setColor(QColor(CYAN if self.value < 75 else level(self.value)[1])); p.setPen(pen)
            p.drawArc(r, 225 * 16, int(-270 * 16 * min(self.value, 100) / 100))
        p.setPen(QColor("#fff")); p.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        p.drawText(QRectF(0, 30, 112, 32), Qt.AlignmentFlag.AlignCenter, "–" if self.value is None else f"{self.value:.0f}%")
        p.setPen(QColor("#8a8790")); p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(0, 60, 112, 18), Qt.AlignmentFlag.AlignCenter, self.sub)

class Bar(QWidget):
    def __init__(self, color="#f2f2f4", h=8):
        super().__init__(); self.h, self.color, self.value = h, color, 0; self.setFixedHeight(h)
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing); p.setPen(Qt.PenStyle.NoPen)
        r = self.h / 2
        p.setBrush(QColor("#26242a")); p.drawRoundedRect(QRectF(self.rect()), r, r)
        p.setBrush(QColor(self.color))
        p.drawRoundedRect(QRectF(0, 0, self.width() * min(max(self.value or 0, 0), 100) / 100, self.h), r, r)

class Spark(QWidget):
    def __init__(self, colors=(PURPLE,), fixed=None, fmt=None, n=90):
        super().__init__(); self.colors, self.fixed = colors, fixed
        self.fmt = fmt or (lambda v: f"{v:.0f}%")
        self.series = [deque([0] * n, maxlen=n) for _ in colors]; self.setMinimumHeight(90)
    def push(self, *vals):
        for s, v in zip(self.series, vals): s.append(v or 0)
        self.update()
    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        m = self.fixed or max(1, max(max(s) for s in self.series))
        if self.fixed:
            p.setPen(QPen(QColor("#222025"), 1))
            for f in (.25, .5, .75): p.drawLine(0, int(h * f), w, int(h * f))
        p.setPen(QColor("#6b6872")); p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(0, 0, w - 4, 14), Qt.AlignmentFlag.AlignRight, self.fmt(m))
        for s, col in zip(self.series, self.colors):
            pts = [QPointF(i * w / (len(s) - 1), h - 3 - v / m * (h - 18)) for i, v in enumerate(s)]
            fill = QPainterPath(QPointF(0, h))
            for pt in pts: fill.lineTo(pt)
            fill.lineTo(w, h); fill.closeSubpath()
            c = QColor(col); g = QLinearGradient(0, 0, 0, h)
            c.setAlpha(110); g.setColorAt(0, c); c.setAlpha(0); g.setColorAt(1, c)
            p.fillPath(fill, QBrush(g))
            line = QPainterPath(pts[0])
            for pt in pts[1:]: line.lineTo(pt)
            p.setPen(QPen(QColor(col), 2)); p.setBrush(Qt.BrushStyle.NoBrush); p.drawPath(line)

class CoreBars(QWidget):
    def __init__(self):
        super().__init__(); self.v = []; self.setMinimumHeight(120)
    def paintEvent(self, e):
        if not self.v: return
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing); p.setPen(Qt.PenStyle.NoPen)
        w = self.width() / len(self.v); h = self.height() - 16
        for i, v in enumerate(self.v):
            x, bw = i * w + 2, max(w - 4, 3)
            p.setBrush(QColor("#26242a")); p.drawRoundedRect(QRectF(x, 0, bw, h), 3, 3)
            p.setBrush(QColor(CYAN if v < 75 else level(v)[1])); fh = h * v / 100
            p.drawRoundedRect(QRectF(x, h - fh, bw, fh), 3, 3)
            p.setPen(QColor("#8a8790")); p.setFont(QFont("Segoe UI", 7))
            p.drawText(QRectF(x - 3, h + 2, bw + 6, 13), Qt.AlignmentFlag.AlignCenter, str(i)); p.setPen(Qt.PenStyle.NoPen)

class ProcList(QWidget):
    def __init__(self, n):
        super().__init__(); g = QGridLayout(self); g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(12); g.setVerticalSpacing(10); g.setColumnStretch(1, 1); self.rows = []
        for i in range(n):
            a = QLabel(); a.setFixedWidth(180); b = Bar(CYAN, 6)
            c = QLabel(); c.setObjectName("muted"); c.setMinimumWidth(100); c.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            g.addWidget(a, i, 0); g.addWidget(b, i, 1); g.addWidget(c, i, 2); self.rows.append((a, b, c))
        g.setRowStretch(n, 1)
    def set(self, items):
        for i, (a, b, c) in enumerate(self.rows):
            show = i < len(items) or (i == 0 and not items)
            for w in (a, b, c): w.setVisible(show)
            if i < len(items):
                n, f, t = items[i]; a.setText(n); b.value = f * 100; b.update(); c.setText(t)
            elif i == 0:
                a.setText("Keine Daten"); b.setVisible(False); c.setVisible(False)

class Chips(QWidget):
    def __init__(self, names):
        super().__init__(); lay = QHBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(14); self.v = []
        for n in names:
            c = Card(); cl = QVBoxLayout(c); cl.setContentsMargins(16, 10, 16, 12)
            l = QLabel(n); l.setObjectName("muted"); v = QLabel("–"); v.setObjectName("h2")
            cl.addWidget(l); cl.addWidget(v); lay.addWidget(c); self.v.append(v)
    def set(self, vals):
        for v, t in zip(self.v, vals): v.setText(t)

class Detail(QWidget):
    back = pyqtSignal()
    def __init__(self, title, sub, chips, legend, colors, fixed=None, fmt=None,
                 proc_title=None, proc_n=10, extra=None, extra_title=""):
        super().__init__(); lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(16)
        b = QPushButton("←  Zurück"); b.setCursor(Qt.CursorShape.PointingHandCursor); b.clicked.connect(self.back)
        t = QLabel(title); t.setObjectName("h1"); s = QLabel(sub); s.setObjectName("muted")
        tl = QVBoxLayout(); tl.setSpacing(0); tl.addWidget(s); tl.addWidget(t)
        head = QHBoxLayout(); head.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter); head.addSpacing(12); head.addLayout(tl); head.addStretch()
        self.chips = Chips(chips)
        gc = Card(); gl = QVBoxLayout(gc); gl.setContentsMargins(18, 14, 18, 14)
        lg = QLabel("&nbsp;&nbsp;&nbsp;".join(f"<span style='color:{c}'>●</span> {n}" for n, c in legend)); lg.setObjectName("muted")
        self.graph = Spark(colors, fixed, fmt); gl.addWidget(lg); gl.addWidget(self.graph)
        left = QVBoxLayout(); left.setSpacing(16); left.addWidget(gc, 2)
        self.extra, self.procs = extra, None
        if extra is not None:
            ec = Card(); el = QVBoxLayout(ec); el.setContentsMargins(18, 14, 18, 14)
            et = QLabel(extra_title); et.setObjectName("h2"); el.addWidget(et); el.addWidget(extra); left.addWidget(ec, 2)
        body = QHBoxLayout(); body.setSpacing(16); body.addLayout(left, 3)
        if proc_title:
            pc = Card(); pl = QVBoxLayout(pc); pl.setContentsMargins(18, 14, 18, 14)
            pt = QLabel(proc_title); pt.setObjectName("h2"); self.procs = ProcList(proc_n)
            pl.addWidget(pt); pl.addSpacing(6); pl.addWidget(self.procs); pl.addStretch(); body.addWidget(pc, 3)
        lay.addLayout(head); lay.addWidget(self.chips); lay.addLayout(body, 1)

class HwCard(Card):
    def __init__(self, title, sub):
        super().__init__(click=True)
        t = QLabel(title); t.setObjectName("h2")
        self.sub = QLabel(sub); self.sub.setObjectName("muted")
        self.status = QLabel(); self.gauge = Gauge(); self.bar = Bar()
        u = QLabel("Auslastung  ·  Details ›"); u.setObjectName("muted"); self.pct = QLabel("–")
        left = QVBoxLayout(); [left.addWidget(w) for w in (t, self.sub, self.status)]; left.addStretch()
        top = QHBoxLayout(); top.addLayout(left); top.addStretch(); top.addWidget(self.gauge)
        row = QHBoxLayout(); row.addWidget(u); row.addStretch(); row.addWidget(self.pct)
        lay = QVBoxLayout(self); lay.setContentsMargins(20, 16, 20, 18)
        lay.addLayout(top); lay.addLayout(row); lay.addWidget(self.bar)
    def update_values(self, v, gauge_sub=""):
        txt, col = level(v)
        self.status.setText(dot(col, txt))
        self.gauge.value, self.gauge.sub, self.bar.value = v, gauge_sub, v or 0
        self.pct.setText("–" if v is None else f"{v:.0f}%")
        self.gauge.update(); self.bar.update()

class Dashboard(QWidget):
    def __init__(self):
        super().__init__(); self.setWindowTitle("Starkins Dashboard"); self.setFixedSize(1360, 820)
        self.setStyleSheet(QSS)
        self.drive = os.environ.get("SystemDrive", "C:") + "\\"
        total_ram = psutil.virtual_memory().total / 2**30
        total_disk = psutil.disk_usage(self.drive).total / 2**30
        cname, gname = cpu_name(), gpu_name()
        self.cpu = HwCard("Prozessor", cname); self.gpu = HwCard("Grafikkarte", gname)
        self.ram = HwCard("Arbeitsspeicher", f"{total_ram:.0f} GB installiert")
        self.disk = HwCard("Speicher", f"{self.drive}  ·  {total_disk:.0f} GB")

        wc = Card(); wl = QVBoxLayout(wc); wl.setContentsMargins(22, 18, 22, 20)
        hi = QLabel("Willkommen zurück"); hi.setObjectName("muted")
        nm = QLabel(getpass.getuser().capitalize()); nm.setObjectName("h1")
        self.w1, self.w2, self.w3 = QLabel(), QLabel(), QLabel()
        wl.addWidget(hi); wl.addWidget(nm); wl.addStretch()
        for w in (self.w1, self.w2, self.w3): wl.addWidget(w); wl.addSpacing(8)

        nc = Card(click=True); nl = QVBoxLayout(nc); nl.setContentsMargins(20, 16, 20, 14)
        nt = QLabel("Netzwerk  ·  Details ›"); nt.setObjectName("h2")
        self.net_status, self.net_speed = QLabel(), QLabel()
        self.spark = Spark((PURPLE,), None, speed, 60)
        head = QHBoxLayout(); head.addWidget(nt); head.addStretch(); head.addWidget(self.net_speed)
        nl.addLayout(head); nl.addWidget(self.net_status); nl.addWidget(self.spark)

        grid = QGridLayout(); grid.setSpacing(16)
        grid.addWidget(wc, 0, 0, 2, 1); grid.addWidget(self.cpu, 0, 1); grid.addWidget(self.ram, 0, 2)
        grid.addWidget(self.gpu, 1, 1); grid.addWidget(self.disk, 1, 2); grid.addWidget(nc, 2, 0, 1, 3)
        for c in range(3): grid.setColumnStretch(c, 3)
        pc = QLabel(platform.node()); pc.setObjectName("muted")
        title = QLabel("Home"); title.setObjectName("h1")
        home = QWidget(); hl = QVBoxLayout(home); hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(pc); hl.addWidget(title); hl.addLayout(grid)

        self.pg = {
            "cpu": Detail("Prozessor", cname, ["Auslastung", "Takt", "Kerne / Threads", "Prozesse"], [("Gesamt", CYAN)], (CYAN,),
                          fixed=100, proc_title="Top-Prozesse nach CPU", extra=CoreBars(), extra_title="Auslastung pro Thread"),
            "gpu": Detail("Grafikkarte", gname, ["Auslastung", "Temperatur", "VRAM", "Leistung"],
                          [("Auslastung", CYAN), ("VRAM", PURPLE)], (CYAN, PURPLE), fixed=100, proc_title="Prozesse auf der GPU"),
            "ram": Detail("Arbeitsspeicher", f"{total_ram:.0f} GB installiert", ["Belegt", "Verfügbar", "Gesamt", "Auslagerung"],
                          [("Belegt", CYAN)], (CYAN,), fixed=100, proc_title="Top-Prozesse nach Speicher"),
            "disk": Detail("Speicher", "Laufwerke & Aktivität", ["Lesen", "Schreiben", "Frei", "Belegt"],
                           [("Lesen", CYAN), ("Schreiben", PURPLE)], (CYAN, PURPLE), fmt=speed,
                           proc_title="Top-Prozesse nach Festplattenaktivität", extra=ProcList(6), extra_title="Laufwerke"),
            "net": Detail("Netzwerk", "Alle Adapter", ["Download", "Upload", "Empfangen gesamt", "Gesendet gesamt"],
                          [("Download", CYAN), ("Upload", PURPLE)], (CYAN, PURPLE), fmt=speed,
                          proc_title="Netzwerkadapter", proc_n=6),
        }
        self.stack = QStackedWidget(); self.stack.addWidget(home); self.pages = {"home": home}
        for k, d in self.pg.items():
            self.stack.addWidget(d); self.pages[k] = d; d.back.connect(lambda: self.go("home"))
        for k, c in (("cpu", self.cpu), ("gpu", self.gpu), ("ram", self.ram), ("disk", self.disk), ("net", nc)):
            c.clicked.connect(lambda k=k: self.go(k))

        side = QFrame(); side.setObjectName("side"); side.setFixedWidth(210)
        sl = QVBoxLayout(side); sl.setContentsMargins(16, 20, 16, 16); sl.setSpacing(4)
        logo = QLabel("STARKINS"); logo.setObjectName("logo"); sl.addWidget(logo); sl.addSpacing(18)
        nav = Nav("⌂   Home"); nav.on(True); nav.clicked.connect(lambda: self.go("home")); sl.addWidget(nav)
        sl.addStretch()
        self.overlay_button = QPushButton("Gaming Overlay: Aus")
        self.overlay_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_button.clicked.connect(self.toggle_overlay)
        sl.addWidget(self.overlay_button)
        self.update_button = QPushButton("Updates suchen")
        self.update_button.setObjectName("updateButton")
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setEnabled(False)
        self.update_button.clicked.connect(self.on_update_button)
        sl.addWidget(self.update_button)
        ver = QLabel("v" + ".".join(APP_VERSION.split(".")[:2])); ver.setObjectName("muted"); sl.addWidget(ver)
        root = QHBoxLayout(self); root.setContentsMargins(18, 18, 18, 18); root.setSpacing(18)
        root.addWidget(side); root.addWidget(self.stack, 1)

        self.cur, self.m = "home", {}
        self.overlay = GamingOverlay()
        self.overlay.close_requested.connect(self.close_overlay_and_restore_dashboard)
        self.overlay_visible = False
        self.fps_by_pid = {}
        self.presentmon = PresentMonWorker(presentmon_path())
        self.presentmon.fps.connect(self.on_fps_update)
        self.presentmon.status.connect(self.overlay_button.setToolTip)
        self.presentmon.start()
        self.last, self.last_t = psutil.net_io_counters(), time.time()
        self.nic_prev, self.dio_prev = psutil.net_io_counters(pernic=True), psutil.disk_io_counters()
        self.io_prev, self.pt, self.agg = {}, time.time(), {}
        psutil.cpu_percent(); psutil.cpu_percent(percpu=True)
        self.go("home")
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(1000); self.tick()
        self._update_worker = None
        self.latest_release = None
        QTimer.singleShot(900, self.check_for_updates)

    def _start_update_worker(self, worker, handler):
        self._update_worker = worker
        worker.result.connect(handler)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def check_for_updates(self):
        self.update_button.setEnabled(False)
        self.update_button.setText("Suche Updates …")
        self.latest_release = None
        self._start_update_worker(UpdateCheckWorker(self), self.on_update_check_finished)

    def on_update_check_finished(self, result):
        error = result.get("error")
        release = result.get("release")
        if error:
            self.update_button.setText("Erneut suchen")
            self.update_button.setToolTip("Updateprüfung fehlgeschlagen: " + error)
            self.latest_release = None
        elif release:
            self.latest_release = release
            tag = str(release.get("tag_name", "")).lstrip("v")
            self.update_button.setText(f"Update {tag}")
            self.update_button.setToolTip("Klicken, um die geprüfte Version herunterzuladen und zu starten.")
        else:
            self.update_button.setText("Updates suchen")
            self.update_button.setToolTip("Deine App ist aktuell. Klicken, um erneut zu suchen.")
            self.latest_release = None
        self.update_button.setEnabled(True)

    def on_update_button(self):
        if not self.latest_release:
            self.check_for_updates()
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("Lade Update …")
        worker = UpdateDownloadWorker(self.latest_release)
        self._start_update_worker(worker, self.on_update_download_finished)

    def on_update_download_finished(self, result):
        error = result.get("error")
        if error:
            self.update_button.setText("Erneut suchen")
            self.update_button.setToolTip(error)
            self.update_button.setEnabled(True)
            self.latest_release = None
            return
        if start_updated_app(result["path"]):
            self.close()
        else:
            self.update_button.setText("Erneut versuchen")
            self.update_button.setEnabled(True)

    def toggle_overlay(self):
        self.overlay_visible = not self.overlay_visible
        if self.overlay_visible:
            self.overlay.update_metrics(self.m, self.current_fps())
            self.overlay.show()
            self.overlay_button.setText("Gaming Overlay: An")
        else:
            self.overlay.hide()
            self.overlay_button.setText("Gaming Overlay: Aus")

    def on_fps_update(self, pid, value):
        self.fps_by_pid[pid] = (value, time.monotonic())

    def close_overlay_and_restore_dashboard(self):
        self.overlay.hide()
        self.overlay_visible = False
        self.overlay_button.setText("Gaming Overlay: Aus")
        if not self.isVisible():
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def current_fps(self):
        sample = self.fps_by_pid.get(foreground_pid())
        if not sample or time.monotonic() - sample[1] > 2.5:
            return None
        return sample[0]

    def closeEvent(self, event):
        if self.overlay_visible:
            event.ignore()
            self.hide()
            return
        self.overlay.close()
        if self.presentmon.isRunning():
            self.presentmon.stop()
            self.presentmon.wait(1500)
        super().closeEvent(event)

    def go(self, k):
        self.cur = k; self.stack.setCurrentWidget(self.pages[k])
        if k in ("cpu", "ram", "disk"): self.sample_procs()
        self.fill()

    def sample_procs(self):
        n = psutil.cpu_count() or 1; now = time.time(); dt = max(now - self.pt, 0.001); self.pt = now
        agg, io_new = {}, {}
        for p in psutil.process_iter():
            with p.oneshot():
                name = safe(p.name, "?")
                if name == "System Idle Process": continue
                cpu = safe(lambda: p.cpu_percent(None)) / n
                rss = safe(lambda: p.memory_info().rss)
                tot = safe(lambda: (lambda io: io.read_bytes + io.write_bytes)(p.io_counters()), None)
            d = 0
            if tot is not None:
                io_new[p.pid] = tot; d = max(tot - self.io_prev.get(p.pid, tot), 0) / dt
            a = agg.setdefault(name, [0, 0.0, 0, 0.0]); a[0] += 1; a[1] += cpu; a[2] += rss; a[3] += d
        self.io_prev, self.agg = io_new, agg

    def top(self, idx, fmt, n=10):
        items = sorted(self.agg.items(), key=lambda kv: kv[1][idx], reverse=True)[:n]
        items = [(k, v) for k, v in items if v[idx] > 0]
        mx = max([v[idx] for k, v in items] or [1])
        return [(k[:22] + (f" ({v[0]})" if v[0] > 1 else ""), v[idx] / mx, fmt(v[idx])) for k, v in items]

    def gpu_procs(self):
        pids = {}
        if GPU:
            for fn in (pynvml.nvmlDeviceGetGraphicsRunningProcesses, pynvml.nvmlDeviceGetComputeRunningProcesses):
                try:
                    for pr in fn(GPU): pids[pr.pid] = max(pids.get(pr.pid, 0), pr.usedGpuMemory or 0)
                except Exception: pass
        agg = {}
        for pid, mem in pids.items():
            try: nm = psutil.Process(pid).name()
            except psutil.Error: nm = f"PID {pid}"
            agg[nm] = agg.get(nm, 0) + mem
        mx = max(agg.values(), default=0) or 1
        return [(k[:22], v / mx, size(v) if v else "aktiv") for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:10]]

    def tick(self):
        m = self.m
        m["cpu"], m["cores"] = psutil.cpu_percent(), psutil.cpu_percent(percpu=True)
        try: m["ghz"] = f"{psutil.cpu_freq().current / 1000:.1f} GHz"
        except Exception: m["ghz"] = ""
        self.cpu.update_values(m["cpu"], m["ghz"])
        vm = m["vm"] = psutil.virtual_memory(); sw = m["sw"] = psutil.swap_memory()
        self.ram.update_values(vm.percent, size(vm.used))
        du = m["du"] = psutil.disk_usage(self.drive)
        self.disk.update_values(du.percent, f"{du.free / 2**30:.0f} GB frei")
        gv = gs = None; gtemp = vram = power = "–"; vpct = 0
        if GPU:
            try: gv = pynvml.nvmlDeviceGetUtilizationRates(GPU).gpu
            except Exception: pass
            try: gtemp = f"{pynvml.nvmlDeviceGetTemperature(GPU, pynvml.NVML_TEMPERATURE_GPU)} °C"; gs = gtemp
            except Exception: pass
            try:
                mi = pynvml.nvmlDeviceGetMemoryInfo(GPU); vpct = mi.used / mi.total * 100
                vram = f"{mi.used / 2**30:.1f} / {mi.total / 2**30:.0f} GB"
            except Exception: pass
            try: power = f"{pynvml.nvmlDeviceGetPowerUsage(GPU) / 1000:.0f} W"
            except Exception: pass
        m["gv"], m["gtemp"], m["vram"], m["power"] = gv, gtemp, vram, power
        self.gpu.update_values(gv, gs or "")

        now = time.time(); dt = max(now - self.last_t, 0.001); self.last_t = now
        io = psutil.net_io_counters(); m["io"] = io
        down, up = (io.bytes_recv - self.last.bytes_recv) / dt, (io.bytes_sent - self.last.bytes_sent) / dt
        self.last = io; m["down"], m["up"] = down, up
        pn, stats, nics = psutil.net_io_counters(pernic=True), psutil.net_if_stats(), []
        for name, c in pn.items():
            s = stats.get(name); pv = self.nic_prev.get(name)
            if not s or not s.isup or not pv or "loopback" in name.lower(): continue
            nics.append((name, (c.bytes_recv - pv.bytes_recv) / dt, (c.bytes_sent - pv.bytes_sent) / dt))
        self.nic_prev, m["nics"] = pn, nics
        dio = psutil.disk_io_counters()
        rd = (dio.read_bytes - self.dio_prev.read_bytes) / dt if dio else 0
        wr = (dio.write_bytes - self.dio_prev.write_bytes) / dt if dio else 0
        self.dio_prev, m["rd"], m["wr"] = dio, rd, wr

        self.spark.push(down)
        self.net_speed.setText(f"↑ {speed(up)}    ↓ {speed(down)}")
        online = any(s.isup for n, s in stats.items() if "loopback" not in n.lower())
        self.net_status.setText(dot(GREEN if online else RED, "Verbunden" if online else "Getrennt"))
        up_s = int(time.time() - psutil.boot_time()); d, r = divmod(up_s, 86400)
        self.w1.setText(dot(GREEN, "System", f"{platform.system()} {platform.release()}"))
        self.w2.setText(dot(GREEN, "Laufzeit", f"{d} T {r // 3600} Std {r % 3600 // 60} Min"))
        self.w3.setText(dot(GREEN, "Prozesse", f"{len(psutil.pids())} aktiv"))

        self.pg["cpu"].graph.push(m["cpu"]); self.pg["gpu"].graph.push(gv, vpct)
        self.pg["ram"].graph.push(vm.percent); self.pg["disk"].graph.push(rd, wr); self.pg["net"].graph.push(down, up)
        if self.overlay_visible:
            self.overlay.update_metrics(m, self.current_fps())
        if self.cur in ("cpu", "ram", "disk"): self.sample_procs()
        self.fill()

    def fill(self):
        k, m = self.cur, self.m
        if k == "home" or not m: return
        d = self.pg[k]
        if k == "cpu":
            d.chips.set([f"{m['cpu']:.0f}%", m["ghz"] or "–",
                         f"{psutil.cpu_count(logical=False) or '?'} / {psutil.cpu_count()}", str(len(psutil.pids()))])
            d.extra.v = m["cores"]; d.extra.update(); d.procs.set(self.top(1, lambda v: f"{v:.1f} %"))
        elif k == "gpu":
            d.chips.set(["–" if m["gv"] is None else f"{m['gv']:.0f}%", m["gtemp"], m["vram"], m["power"]])
            d.procs.set(self.gpu_procs())
        elif k == "ram":
            vm, sw = m["vm"], m["sw"]
            d.chips.set([size(vm.used), size(vm.available), size(vm.total), f"{sw.percent:.0f}%"])
            d.procs.set(self.top(2, size))
        elif k == "disk":
            du = m["du"]
            d.chips.set([speed(m["rd"]), speed(m["wr"]), f"{du.free / 2**30:.0f} GB", f"{du.percent:.0f}%"])
            rows = []
            for part in psutil.disk_partitions():
                try: u = psutil.disk_usage(part.mountpoint)
                except Exception: continue
                rows.append((part.mountpoint, u.percent / 100, f"{u.used / 2**30:.0f} / {u.total / 2**30:.0f} GB"))
            d.extra.set(rows[:6]); d.procs.set(self.top(3, speed))
        elif k == "net":
            io = m["io"]
            d.chips.set([speed(m["down"]), speed(m["up"]), size(io.bytes_recv), size(io.bytes_sent)])
            nics = sorted(m["nics"], key=lambda x: -(x[1] + x[2]))[:6]; mx = max([x[1] + x[2] for x in nics] or [1]) or 1
            d.procs.set([(n[:24], (a + b) / mx, f"↓ {speed(a)}  ↑ {speed(b)}") for n, a, b in nics])

if __name__ == "__main__":
    app = QApplication(sys.argv); win = Dashboard(); win.show(); sys.exit(app.exec())





