# hatework_station_v2_oneway.py
# Hatework Station (One-way West->East) – SCADA-style
# - Two tracks: MAIN (bottom), LOOP (top)
# - Can park on both; if MAIN occupied -> cannot park LOOP
# - Call In MAIN / LOOP, Call Out (MAIN first), Emergency Release
# - Signals: RED (MAIN parked), YELLOW (checking/route active), GREEN (free)
# - Fade-like animation on signal lamps + soft control-room beep
# ---------------------------------------------------------------

import sys, math, time, os, wave, struct, tempfile
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import (
    Qt, QTimer, QPointF, QRectF, QEasingCurve, Property, QObject, QUrl, QPropertyAnimation
)
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont
)
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import (
    QApplication, QGraphicsView, QGraphicsScene, QGraphicsItem, QGraphicsEllipseItem,
    QGraphicsRectItem, QGraphicsSimpleTextItem, QGraphicsTextItem, QLabel, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFrame, QMainWindow
)



# ------------ Theme / Constants ------------
COL_BG      = QColor("#0f1015")
COL_TRACK   = QColor("#a0a3aa")
COL_ACTIVE  = QColor("#ff3b3b")
COL_TEXT    = QColor("#e8e9f0")
COL_LABEL   = QColor("#b7bdd9")
COL_GREEN   = QColor("#00ff00")
COL_YELLOW  = QColor("#ffd400")
COL_RED     = QColor("#ff0000")
COL_CYAN    = QColor("#3bd7ff")
COL_BLUE    = QColor("#6aa6ff")
COL_BOX     = QColor("#ffd84d")

VIEW_W, VIEW_H = 1280, 720

# Timing (scaled for demo)
APPROACH_TIME = 2.0   # “checking/route setting” ~2s
DWELL_FRAMES  = 90    # platform dwell ~3s @30fps

# ------------ Utility: soft beep ------------
def ensure_soft_beep_wav() -> str:
    path = os.path.join(tempfile.gettempdir(), "soft_beep_hatework.wav")
    if os.path.exists(path):
        return path
    framerate = 44100
    duration = 0.14  # seconds
    freq = 880.0
    amplitude = 0.2
    nframes = int(duration * framerate)
    with wave.open(path, 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        for i in range(nframes):
            t = i / framerate
            env = min(1.0, i/(0.005*framerate), (nframes - i)/(0.005*framerate))
            sample = int(32767 * amplitude * math.sin(2*math.pi*freq*t) * env)
            wf.writeframes(struct.pack('<h', sample))
    return path

# ------------ Graphics primitives ------------
class TrackItem(QGraphicsItem):
    def __init__(self, x1, y1, x2, y2, name, thickness=7):
        super().__init__()
        self.p1 = QPointF(x1, y1)
        self.p2 = QPointF(x2, y2)
        self.name = name
        self.thick = thickness
        self.occupied = False
        self.reserved = False
        self.setZValue(1)

    def boundingRect(self):
        extra = self.thick/2 + 4
        return QRectF(min(self.p1.x(), self.p2.x())-extra,
                      min(self.p1.y(), self.p2.y())-extra,
                      abs(self.p1.x()-self.p2.x()) + 2*extra,
                      abs(self.p1.y()-self.p2.y()) + 2*extra)

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(COL_TRACK, self.thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        if self.occupied:
            pen.setColor(QColor(255, 90, 90))
        painter.setPen(pen)
        painter.drawLine(self.p1, self.p2)
        if self.reserved and not self.occupied:
            hl = QPen(COL_ACTIVE, self.thick-3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(hl)
            painter.drawLine(self.p1, self.p2)

# -------- Signal Lamps with fade (QObject + QGraphicsEllipseItem) --------
class SignalLamp(QObject, QGraphicsEllipseItem):
    def __init__(self, x, y, radius=10, gfx_parent=None):
        QObject.__init__(self)  # do not pass a non-QObject parent
        QGraphicsEllipseItem.__init__(self, -radius, -radius, radius*2, radius*2)
        if gfx_parent is not None:
            self.setParentItem(gfx_parent)   # graphics parent
        self.setPos(x, y)
        self.color = QColor(255, 0, 0)
        self.opacity_val = 1.0
        self.setZValue(9)

        self._fade_anim = QPropertyAnimation(self, b"lampOpacity", self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setEasingCurve(QEasingCurve.InOutQuad)

    def setColor(self, col: QColor):
        self.color = col
        self.update()

    def fadeTo(self, target: float):
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self.opacity_val)
        self._fade_anim.setEndValue(target)
        self._fade_anim.start()

    def getLampOpacity(self): return self.opacity_val
    def setLampOpacity(self, v): self.opacity_val = float(v); self.update()
    lampOpacity = Property(float, fget=getLampOpacity, fset=setLampOpacity)

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.Antialiasing)
        c = QColor(self.color); c.setAlphaF(self.opacity_val)
        painter.setPen(Qt.NoPen); painter.setBrush(QBrush(c))
        painter.drawEllipse(self.rect())

# -------- Signal Head (สามดวง R/Y/G) --------
class SignalHead(QGraphicsItem):
    def __init__(self, x, y, name, beep: QSoundEffect):
        super().__init__()
        self.name = name
        self.beep = beep
        self.R = SignalLamp(0, 0, 10, self)
        self.Y = SignalLamp(0, 22, 10, self)
        self.G = SignalLamp(0, 44, 10, self)
        self.setPos(x, y)
        self.setZValue(8)
        self.aspect = "RED"
        self.update_aspect_immediate("RED")

    def boundingRect(self):
        return QRectF(-14, -4, 28, 60)

    def paint(self, p, o, w):
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(40, 45, 70)))
        p.drawRoundedRect(-14, -4, 28, 60, 6, 6)

    def update_aspect_immediate(self, aspect):
        self.aspect = aspect
        self.R.setColor(COL_RED); self.Y.setColor(COL_YELLOW); self.G.setColor(COL_GREEN)
        for lamp in (self.R, self.Y, self.G): lamp.setLampOpacity(0.18)
        {"GREEN": self.G, "YELLOW": self.Y, "RED": self.R}[aspect].setLampOpacity(1.0)

    def set_aspect(self, aspect):
        if aspect == self.aspect: return
        self.aspect = aspect
        # simple stepped fade
        for step in range(0, 6):
            v_on  = 0.18 + (1.0-0.18)*(step/5.0)
            v_off = 1.0 - (1.0-0.18)*(step/5.0)
            if aspect == "GREEN":
                self.R.setLampOpacity(v_off); self.Y.setLampOpacity(v_off); self.G.setLampOpacity(v_on)
            elif aspect == "YELLOW":
                self.R.setLampOpacity(v_off); self.Y.setLampOpacity(v_on);  self.G.setLampOpacity(v_off)
            else:
                self.R.setLampOpacity(v_on);  self.Y.setLampOpacity(v_off); self.G.setLampOpacity(v_off)
            QApplication.processEvents()
        self.beep.play()

# ------------ Train ------------
class TrainItem(QGraphicsItem):
    def __init__(self, path_points:list[QPointF], train_id:str, speed_px=3.4):
        super().__init__()
        self.path = path_points
        self.idx = 0
        self.speed_px = speed_px
        self.in_dwell = False
        self.dwell_counter = 0
        self.train_id = train_id
        self.setZValue(20)
        if self.path:
            self.setPos(self.path[0])
        # anti re-capture flags
        self.skip_platform_ticks = 0
        self.has_dwelled_main = False
        self.has_dwelled_loop = False

    def boundingRect(self):
        return QRectF(-18, -10, 36, 20)

    def paint(self, p, o, w):
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        # body
        p.setBrush(QBrush(QColor("#e6e7eb")))
        p.drawRoundedRect(-18, -8, 36, 16, 5, 5)
        # nose red
        p.setBrush(QBrush(QColor(220, 50, 50)))
        p.drawRoundedRect(8, -8, 10, 16, 4, 4)
        # windows
        p.setBrush(QBrush(QColor(80, 110, 160)))
        p.drawRoundedRect(-10, -5, 10, 10, 2, 2)
        p.drawRoundedRect(2, -5, 10, 10, 2, 2)

    def step(self):
        if not self.path or self.idx >= len(self.path) - 1:
            return True
        if self.in_dwell or self.speed_px <= 0:
            return False
        x, y = self.pos().x(), self.pos().y()
        tx, ty = self.path[self.idx + 1].x(), self.path[self.idx + 1].y()
        dx, dy = tx - x, ty - y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            self.idx += 1
            return self.idx >= len(self.path) - 1
        step = min(d, self.speed_px)
        self.setPos(x + dx / d * step, y + dy / d * step)
        ang = math.degrees(math.atan2(dy, dx))
        self.setRotation(ang)
        if step >= d - 1e-6:
            self.idx += 1
            return self.idx >= len(self.path) - 1
        return False

# ------------ Main Window ------------
@dataclass
class Route:
    tracks: list[str]
    overlap: list[str]
    low_speed: bool = False

class HateworkWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("สถานีเฮทเวิร์ค / Hatework Station – One-way Simulator")
        self.resize(VIEW_W, VIEW_H)

        # Scene & View
        self.scene = QGraphicsScene(0, 0, VIEW_W-20, VIEW_H-120)
        self.view = QGraphicsView(self.scene)
        self.view.setBackgroundBrush(QBrush(COL_BG))
        self.view.setRenderHint(QPainter.Antialiasing)

        # Buttons
        ctr = QWidget(); ctr_l = QHBoxLayout(ctr)
        ctr_l.setContentsMargins(8, 8, 8, 8)
        self.btn_start = QPushButton("เริ่มจำลอง / Start")
        self.btn_call_in_main  = QPushButton("เรียกรถเข้า MAIN / Call In MAIN")
        self.btn_call_in_loop  = QPushButton("เรียกรถเข้า LOOP / Call In LOOP")
        self.btn_call_out      = QPushButton("ปล่อยรถออก / Call Out (MAIN first)")
        self.btn_emg           = QPushButton("ปลดฉุกเฉิน / Emergency Release")
        for b in (self.btn_start, self.btn_call_in_main, self.btn_call_in_loop, self.btn_call_out, self.btn_emg):
            ctr_l.addWidget(b)

        root = QWidget(); lay = QVBoxLayout(root)
        lay.addWidget(self.view)
        sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setStyleSheet("color:#2a315b")
        lay.addWidget(sep); lay.addWidget(ctr)
        self.setCentralWidget(root)

        # Header labels
        self.h_title = QGraphicsSimpleTextItem("สถานีเฮทเวิร์ค / Hatework Station")
        self.h_title.setFont(QFont("Tahoma", 22, QFont.Bold)); self.h_title.setBrush(QBrush(COL_GREEN)); self.h_title.setPos(420, 6); self.scene.addItem(self.h_title)
        self.h_operator = QGraphicsSimpleTextItem("ผู้ควบคุม / Operator: สมชาย ใจดี")
        self.h_operator.setFont(QFont("Tahoma", 12)); self.h_operator.setBrush(QBrush(COL_LABEL)); self.h_operator.setPos(20, 10); self.scene.addItem(self.h_operator)
        self.h_clock = QGraphicsSimpleTextItem("--:--:--")
        self.h_clock.setFont(QFont("Consolas", 18)); self.h_clock.setBrush(QBrush(COL_CYAN)); self.h_clock.setPos(20, 40); self.scene.addItem(self.h_clock)
        self.msg_area = QGraphicsSimpleTextItem("")
        self.msg_area.setFont(QFont("Consolas", 12)); self.msg_area.setBrush(QBrush(COL_BLUE)); self.msg_area.setPos(20, self.scene.height()-30); self.scene.addItem(self.msg_area)

        # Yellow status boxes
                # Yellow status boxes — ใช้ QLabel ผ่าน proxy ให้ข้อความแสดงและจัดกึ่งกลางแน่นอน
        def make_box(x, y, text):
            box_w, box_h = 240, 68
            pad = 10

            # วาดกล่องพื้นหลัง
            box = QGraphicsRectItem(0, 0, box_w, box_h)
            box.setPos(x, y)
            box.setZValue(3)
            box.setBrush(QBrush(COL_BOX))
            box.setPen(QPen(QColor("#caa322"), 2))
            self.scene.addItem(box)

            # สร้าง QLabel สำหรับข้อความ (ห่อบรรทัด + จัดกึ่งกลาง)
            label = QLabel()
            # ใช้ HTML สั้น ๆ เพื่อรองรับไทย/อังกฤษและขึ้นบรรทัด
            label.setText(
                f"<div style='text-align:center; font-family:Tahoma; "
                f"font-weight:700; font-size:12px; color:#222;'>{text.replace(chr(10), '<br>')}</div>"
            )
            label.setWordWrap(True)
            label.setFixedSize(box_w - 2*pad, box_h - 2*pad)
            label.setStyleSheet("background: transparent;")  # โปร่งใสให้เห็นสีเหลืองของกล่อง

            # ฝังเข้า scene แล้วผูกเป็นลูกของกล่อง
            proxy = self.scene.addWidget(label)
            proxy.setParentItem(box)
            proxy.setZValue(box.zValue() + 1)
            proxy.setPos(pad, pad)


        make_box(20,  self.scene.height()-110, "00003\nPoints Emergency / ฉุกเฉินจุดตัด")
        make_box(280, self.scene.height()-110, "00003\nPoints Failed / จุดตัดขัดข้อง")
        make_box(540, self.scene.height()-110, "00003\nEmergency Route Release / ปลดเส้นทาง")



            # --- Tracks layout (Main + Loop with straight-then-diagonal branches) ---
        self.tracks: dict[str, TrackItem] = {}

        # Main line (ซอยเป็นท่อนให้ต่อกิ่งพอดี)
        self._add_track("M_ENTRY",   60, 300, 200, 300)     # ทางเข้าจากซ้าย
        self._add_track("M_WSTRA",  200, 300, 260, 300)     # ตรงต่อถึงจุดแยกซ้าย (ใหม่: ขยายถึง x=260)
        self._add_track("M_CENTER", 260, 300, 980, 300)     # ใต้ชานชาลา (เริ่มที่ x=260)
        self._add_track("M_JOIN",   980, 300, 1030, 300)    # ช่วงก่อนกิ่งขวา
        self._add_track("M_RJOIN", 1050, 300, 1120, 300)    # หลังต่อกิ่งขวา
        self._add_track("M_EAST",  1120, 300, 1220, 300)    # ทางออกตะวันออก
        self._add_track("M_PATCH", 1030, 300, 1050, 300)   # เติมช่วงที่หายไป


        # Loop (บน) + กิ่งซ้าย/ขวาแบบ “ตรงแล้วเฉียง”
        # ซ้าย: ให้ตรงบน Main ถึง x=260 แล้วเฉียงขึ้นทีเดียว
        self._add_track("BRANCH_W", 260, 300, 300, 250)     # เฉียงขึ้นจาก Main→Loop
        self._add_track("L_MAIN",   300, 250, 1000, 250)    # รางบนแนวนอน

        # ขวา: ให้รางบนตรงถึง x=1000 แล้วเฉียงลงทีเดียว
        self._add_track("BRANCH_E", 1000, 250, 1050, 300)   # เฉียงลงจาก Loop→Main



        # Platforms
        self.platform_x1, self.platform_x2, self.platform_y = 520, 760, 300   # MAIN
        self.platform_loop_x1, self.platform_loop_x2, self.platform_loop_y = 520, 760, 250  # LOOP
        self._add_platform(self.platform_x1, self.platform_x2, self.platform_y, QColor(60, 80, 110, 50), QColor(120,150,200), "ชานชาลาหลัก / Platform (Main)")
        self._add_platform(self.platform_loop_x1, self.platform_loop_x2, self.platform_loop_y, QColor(60,110,80,50), QColor(120,200,150), "ชานชาลารอง / Platform (Loop)")

        # Station labels
        self._label(20, 90, "SOUTH (WEST) / สถานีใต้"); self._label(self.scene.width()-260, 90, "NORTH (EAST) / สถานีเหนือ")
        self._label(80, 322, "101"); self._label(560, 322, "102"); self._label(1100, 322, "103")
        self._label(305, 226, "15"); self._label(640, 226, "16"); self._label(975, 226, "17")

        # Beep sound (use QUrl for Windows path safety)
        self.beep = QSoundEffect()
        self.beep.setLoopCount(1)
        self.beep.setVolume(0.22)
        self.beep.setSource(QUrl.fromLocalFile(ensure_soft_beep_wav()))

        # One entry signal at WEST (left)
        self.signal_W = SignalHead(180, 240, "S_W", self.beep)
        self.scene.addItem(self.signal_W)
        


            # Routes (West -> East)
        self.routes = {
            "MAIN": Route(
                tracks=["M_ENTRY", "M_WSTRA", "M_CENTER", "M_JOIN", "M_PATCH", "M_RJOIN", "M_EAST"],
                overlap=[],
                low_speed=False
            ),
            "LOOP": Route(
                tracks=["M_ENTRY", "M_WSTRA", "BRANCH_W", "L_MAIN", "BRANCH_E", "M_RJOIN", "M_EAST"],
                overlap=[],
                low_speed=True
            ),
        }




        # States
        self.platform_state = {"MAIN": None, "LOOP": None}  # parked TrainItem or None
        self.queue: deque[str] = deque()  # "MAIN" / "LOOP"
        self.active_path: str|None = None
        self.approach_until = 0.0
        self.trains: list[TrainItem] = []
        self.train_counter = 1
        self.running = False

        # Timers
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick)
        self.clock_timer = QTimer(self); self.clock_timer.timeout.connect(self._update_clock); self.clock_timer.start(250)

        # Wire buttons
        self.btn_start.clicked.connect(self._start)
        self.btn_call_in_main.clicked.connect(lambda: self.call_in("MAIN"))
        self.btn_call_in_loop.clicked.connect(lambda: self.call_in("LOOP"))
        self.btn_call_out.clicked.connect(self.call_out_prioritized)
        self.btn_emg.clicked.connect(self.emergency_release)

        self._apply_styles()
        self.update_signals()  # initial GREEN

    # ---------- helpers ----------
    def _apply_styles(self):
        self.setStyleSheet("""
            QPushButton {
              background: #2a315b; color: #fff; padding: 8px 12px; border-radius: 10px; border: 0;
              font-family: "Segoe UI", "Kanit"; font-size: 13px;
            }
            QPushButton:hover { background: #3a4380; }
            QPushButton:pressed { background: #1e2446; }
        """)

    def _update_clock(self):
        self.h_clock.setText(time.strftime("%H:%M:%S"))

    def _add_track(self, name, x1, y1, x2, y2):
        t = TrackItem(x1,y1,x2,y2,name)
        self.tracks[name] = t; self.scene.addItem(t)

    def _label(self, x, y, text, color=COL_TEXT, size=12):
        t = QGraphicsSimpleTextItem(text); t.setFont(QFont("Tahoma", size))
        t.setBrush(QBrush(color)); t.setPos(x, y); self.scene.addItem(t); return t

    def _add_platform(self, x1, x2, y, fill, pen_col, label):
        r = QGraphicsRectItem(x1, y-10, x2-x1, 20)
        r.setBrush(QBrush(fill)); r.setPen(QPen(pen_col, 1, Qt.DashLine)); r.setZValue(2); self.scene.addItem(r)
        self._label(x1, y-32, label, COL_LABEL, 11)

    def info(self, msg):
        self.msg_area.setText(msg)

    def _start(self):
        if not self.running:
            self.running = True
            self.timer.start(33)
            self.info("เริ่มจำลอง / Simulation started")

    # ---------- signals logic ----------
    def update_signals(self):
        # RED: if MAIN parked
        if self.platform_state["MAIN"] is not None:
            self.signal_W.set_aspect("RED"); return
        # YELLOW: during checking/approach
        if self.active_path is not None and time.time() < self.approach_until:
            self.signal_W.set_aspect("YELLOW"); return
        # otherwise GREEN
        self.signal_W.set_aspect("GREEN")

    # ---------- call in / out ----------
    def call_in(self, target: str):
        # Block LOOP if MAIN parked
        if target == "LOOP" and self.platform_state["MAIN"] is not None:
            self.info("ห้ามจอด LOOP เพราะ MAIN มีรถจอดอยู่ / Cannot park LOOP while MAIN occupied")
            return
        # If target already parked -> deny
        if self.platform_state[target] is not None:
            self.info(f"{target} มีรถจอดอยู่แล้ว / {target} already occupied")
            return
        # Enqueue path
        self.queue.append(target)
        self.info(f"เรียกรถเข้า {target} / Call In {target} → เข้าคิว")
        # If idle, trigger dispatch
        if self.active_path is None:
            self._dispatch_if_possible()
        self.update_signals()

    def call_out_prioritized(self):
        # MAIN first
        if self.platform_state["MAIN"] is not None:
            tr = self.platform_state["MAIN"]
            tr.in_dwell = False
            tr.dwell_counter = 0
            tr.speed_px = 3.4
            tr.skip_platform_ticks = 30      # prevent immediate re-capture
            self.platform_state["MAIN"] = None
            self.info(f"{tr.train_id}: Call Out MAIN")
            self.update_signals()
            return

        # LOOP
        if self.platform_state["LOOP"] is not None:
            tr = self.platform_state["LOOP"]
            tr.in_dwell = False
            tr.dwell_counter = 0
            tr.speed_px = 2.6
            tr.skip_platform_ticks = 30
            self.platform_state["LOOP"] = None
            self.info(f"{tr.train_id}: Call Out LOOP")
            self.update_signals()
            return

        self.info("ไม่มีรถจอดในชานชาลา / No train parked")

    # ---------- dispatch ----------
    def _dispatch_if_possible(self):
        if self.active_path is not None or not self.queue:
            return
        path = self.queue.popleft()  # MAIN / LOOP
        rt: Route = self.routes[path]
        # reserve tracks + overlap
        for t in rt.tracks + rt.overlap:
            self.tracks[t].reserved = True; self.tracks[t].update()
        self.active_path = path
        self.approach_until = time.time() + APPROACH_TIME
        self.update_signals()  # YELLOW
        # build path points (West->East)
        pts = []
        for tn in rt.tracks:
            trk = self.tracks[tn]
            pts.extend([trk.p1, trk.p2])
        # spawn train at left
        tid = f"TR{self.train_counter:03d}"; self.train_counter += 1
        speed = 3.4 if not rt.low_speed else 2.6
        train = TrainItem(pts, tid, speed_px=speed)
        self.scene.addItem(train); self.trains.append(train)
        self.info(f"{tid} departed on {path}")

    def emergency_release(self):
        # immediate release (demo)
        for t in self.tracks.values():
            t.reserved = False; t.occupied = False; t.update()
        self.active_path = None
        self.approach_until = 0
        self.update_signals()
        self.info("Route Released / ปลดเส้นทาง")

    # ---------- platform & tick ----------
    def _check_platform_hit(self, tr: TrainItem):
        # guard to avoid re-capture right after Call Out
        if tr.skip_platform_ticks > 0:
            tr.skip_platform_ticks -= 1
            return

        x, y = tr.pos().x(), tr.pos().y()
        def in_rng(px1, px2, py): return abs(y - py) < 8 and px1 <= x <= px2

        # MAIN platform — allow dwell only once per trip
        if in_rng(self.platform_x1, self.platform_x2, self.platform_y):
            if (self.platform_state["MAIN"] is None and not tr.in_dwell and not tr.has_dwelled_main):
                tr.in_dwell = True
                tr.dwell_counter = DWELL_FRAMES
                tr.speed_px = 0
                tr.has_dwelled_main = True
                self.platform_state["MAIN"] = tr
                self.info(f"{tr.train_id}: Platform MAIN dwell")

        # LOOP platform — allow dwell once and only if MAIN not parked
        if in_rng(self.platform_loop_x1, self.platform_loop_x2, self.platform_loop_y):
            if (self.platform_state["MAIN"] is None and
                self.platform_state["LOOP"] is None and
                not tr.in_dwell and not tr.has_dwelled_loop):
                tr.in_dwell = True
                tr.dwell_counter = DWELL_FRAMES
                tr.speed_px = 0
                tr.has_dwelled_loop = True
                self.platform_state["LOOP"] = tr
                self.info(f"{tr.train_id}: Platform LOOP dwell")

        # dwell countdown (keep parked until Call Out)
        if tr.in_dwell:
            tr.dwell_counter -= 1
            if tr.dwell_counter <= 0:
                tr.speed_px = 0  # stay stopped until Call Out

    def tick(self):
        if not self.running:
            return
        # approach phase -> after timer, set GREEN if eligible
        if self.active_path is not None and time.time() >= self.approach_until:
            self.signal_W.set_aspect("GREEN")

        # move trains
        finished = []
        for tr in self.trains:
            # mark route tracks occupied while train is on path
            if self.active_path:
                for tname in self.routes[self.active_path].tracks:
                    self.tracks[tname].occupied = True
            self._check_platform_hit(tr)
            done = tr.step()
            if done:
                finished.append(tr)

        # clear finished and release route
        if finished:
            for tr in finished:
                self.scene.removeItem(tr)
                if tr in self.trains: self.trains.remove(tr)
                self.info(f"{tr.train_id}: Arrived / ถึงปลายทาง")
            for t in self.tracks.values():
                t.reserved = False; t.occupied = False; t.update()
            self.active_path = None
            self.approach_until = 0
            self.update_signals()
            # dispatch next if queued
            if self.queue:
                self._dispatch_if_possible()

        # refresh
        for t in self.tracks.values():
            t.update()

# ------------ Main ------------
def main():
    app = QApplication(sys.argv)
    win = HateworkWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
