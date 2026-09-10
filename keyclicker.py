"""键盘连点器 —— PyQt6 重构版

功能：
  · 连点模式：按固定间隔持续发送指定按键，支持 ctrl+f9 这类组合键
  · 定时序列：倒计时结束后依次发送一串按键，可循环触发
  · 多配置页：多套互不干扰的任务，配置自动保存（原子写入）
  · 按键录入：连点按键与定时序列都能"按下即录入"，无需手写键名
  · 全局热键：Ctrl+` 开始/停止当前页，F6 全部停止（紧急停止）

运行：
    python keyclicker.py

依赖：
    pip install PyQt6 keyboard

注意：
    keyboard 库在部分游戏/管理员权限窗口中需要以管理员身份运行才能生效。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

__version__ = "1.1.0"
__all__ = [
    "main", "parse_keys", "normalize_key", "validate_keys", "send_keys",
    "ClickTask", "TimerTask", "Page", "KeyRecorder", "KeyCaptureDialog",
    "SequenceEditor", "PagePanel", "MainWindow",
]

try:
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
    from PyQt6.QtGui import QIntValidator
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
        QComboBox, QScrollArea, QFrame, QVBoxLayout, QHBoxLayout, QDialog,
        QMessageBox, QSizePolicy, QStackedWidget, QInputDialog,
    )

    import keyboard
except ImportError as e:
    msg = (f"缺少依赖库，程序无法启动：\n{e}\n\n"
           "请先安装依赖：\n    pip install PyQt6 keyboard\n\n"
           "若 keyboard 库报权限错误，请尝试以管理员身份运行。")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "键盘连点器", 0x10)
    except Exception:
        print(msg, file=sys.stderr)
    sys.exit(1)


# ── 主题色 ──
BG       = "#1e1e2e"
BG_CARD  = "#2a2a3e"
BG_ENTRY = "#363650"
BG_HOVER = "#41415f"
FG       = "#cdd6f4"
FG_DIM   = "#6c7086"
ACCENT   = "#89b4fa"
GREEN    = "#a6e3a1"
RED      = "#f38ba8"
YELLOW   = "#f9e2af"
ORANGE   = "#fab387"
PURPLE   = "#cba6f7"
BORDER   = "#45475a"

FONT_MAIN = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

APP_NAME    = "键盘连点器"
# 打包成 exe 后 __file__ 指向临时解压目录，需改用 exe 所在目录存放配置
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, "clicker_config.json")

# ── 按键名规范化 ──
# keyboard 库有自己的一套规范名（ctrl / windows / page up / caps lock…），
# 下面只补齐库本身不认识的常见写法，其余原样交给库解析。
KEY_ALIAS = {
    "super": "windows", "win": "windows", "cmd": "windows", "command": "windows",
    "winleft": "left windows", "winright": "right windows",
    "ctl": "ctrl", "control": "ctrl",
    "scrolllock": "scroll lock", "prtscn": "print screen", "printscreen": "print screen",
    "pgup": "page up", "pgdn": "page down", "ins": "insert",
}

# 组合键显示顺序（windows 键在库中的规范名就是 windows）
MODIFIER_ORDER = ("ctrl", "shift", "alt", "windows")

# 各模式下按键列表允许的最大长度，防止误粘贴超长文本
MAX_KEYS_PER_STEP = 8

# 连点/序列的最小间隔与最短压键时长（毫秒）
MIN_INTERVAL_MS = 16
MIN_HOLD_MS = 20


def normalize_key(name: str) -> str:
    """把按键名转换成 keyboard 库可识别的名称。"""
    key = (name or "").strip().lower()
    return KEY_ALIAS.get(key, key)


def parse_keys(raw: str) -> list[str]:
    """把 "ctrl+f9" 解析成 ["ctrl", "f9"]。"""
    return [normalize_key(k) for k in re.split(r"\s*\+\s*", raw or "") if k.strip()]


def is_valid_key(name: str) -> bool:
    """单个按键名是否被 keyboard 库识别（不产生任何实际按键）。"""
    try:
        keyboard.key_to_scan_codes(name)
        return True
    except Exception:
        return False


def validate_keys(raw: str) -> tuple[list[str], str | None]:
    """校验按键文本。

    返回 ``(按键列表, 错误信息)``，错误信息为 None 表示合法。
    提前校验可以避免"键名写错 → 任务在跑但一个键都不发"的静默失效。
    """
    keys = parse_keys(raw)
    if not keys:
        return [], "按键不能为空"
    if len(keys) > MAX_KEYS_PER_STEP:
        return keys, f"组合键最多 {MAX_KEYS_PER_STEP} 个按键"
    bad = [k for k in keys if not is_valid_key(k)]
    if bad:
        return keys, "无法识别的按键：" + "、".join(bad)
    return keys, None


def format_keys(keys) -> str:
    """按键列表 → 便于阅读的组合键文本（left ctrl 显示为 ctrl）。"""
    keys = [k for k in keys if k]
    if not keys:
        return ""
    try:
        return keyboard.get_hotkey_name(keys)
    except Exception:
        return "+".join(keys)


def send_keys(keys: list[str], hold_ms: int = 20) -> bool:
    """按下并释放一组按键（组合键）。

    返回是否至少成功发送了一个按键；失败时不再静默吞掉错误，
    上层可据此提示用户（例如键名不合法或权限不足）。
    """
    ks = [k for k in keys if k]
    if not ks:
        return False
    pressed: list[str] = []
    for k in ks:
        try:
            keyboard.press(k)
            pressed.append(k)
        except Exception:
            continue
    if not pressed:
        return False
    time.sleep(max(hold_ms, 8) / 1000.0)
    for k in reversed(pressed):
        try:
            keyboard.release(k)
        except Exception:
            continue
    return True


# ══════════════════════════════════════════════════════════
#  任务模型（后台线程）
# ══════════════════════════════════════════════════════════
class _BaseTask:
    def __init__(self):
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def stop(self):
        self._stop.set()
        self._pause.clear()
        if self._thread:
            self._thread.join(timeout=0.8)
        self._thread = None

    def toggle_pause(self):
        if self._pause.is_set():
            self._pause.clear()
        else:
            self._pause.set()

    def _launch(self, target):
        # 每次启动使用全新的事件对象，避免旧线程未退出时被"复活"
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()


@dataclass
class ClickTask(_BaseTask):
    """连点任务：每隔 interval_ms 发送一次按键（keys 支持 ctrl+f9 组合键）。"""

    keys: str = "a"
    interval_ms: int = 100
    click_count: int = 0

    def __post_init__(self):
        _BaseTask.__init__(self)
        self.keys = "+".join(parse_keys(self.keys)) or "a"
        self.interval_ms = max(int(self.interval_ms), MIN_INTERVAL_MS)

    def start(self):
        if self.running:
            return
        self.click_count = 0
        self._launch(self._loop)

    def _loop(self):
        kp = parse_keys(self.keys)
        next_t = time.monotonic()
        while not self._stop.is_set():
            if self._pause.is_set():
                self._stop.wait(0.05)
                next_t = time.monotonic()  # 暂停恢复后重新对齐节拍
                continue
            send_keys(kp)
            self.click_count += 1
            # 按固定节拍推进，避免每次发送的耗时累积成漂移
            next_t += self.interval_ms / 1000.0
            delay = next_t - time.monotonic()
            if delay <= 0:
                next_t = time.monotonic()
                delay = 0.0
            self._stop.wait(delay)

    def to_dict(self):
        return {"type": "click", "keys": self.keys, "interval": self.interval_ms}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("keys", "a"), int(d.get("interval", 100)))


@dataclass
class TimerTask(_BaseTask):
    """定时序列任务：倒计时结束后依次发送 seq 中的按键，然后重新计时。"""

    seq: list = field(default_factory=lambda: ["a"])
    minutes: int = 0
    seconds: int = 10
    gap_ms: int = 200
    remaining: int = 0
    fire_count: int = 0

    def __post_init__(self):
        _BaseTask.__init__(self)
        self.seq = [s for s in ("+".join(parse_keys(str(s))) for s in self.seq) if s] or ["a"]
        self.minutes = max(int(self.minutes), 0)
        self.seconds = max(int(self.seconds), 0)
        self.gap_ms = max(int(self.gap_ms), MIN_INTERVAL_MS)
        self.total_sec = self.minutes * 60 + self.seconds
        if self.total_sec < 1:
            self.total_sec = 1
        self.remaining = self.total_sec if self.remaining <= 0 else int(self.remaining)

    def start(self):
        if self.running:
            return
        self.remaining = self.total_sec
        self.fire_count = 0
        self._launch(self._loop)

    def _fire(self):
        """依次发送整个按键序列（可被 stop() 中途打断）。"""
        for combo in self.seq:
            if self._stop.is_set():
                return
            send_keys(parse_keys(str(combo)))
            self._stop.wait(self.gap_ms / 1000.0)

    def _loop(self):
        next_tick = time.monotonic() + 1.0
        was_paused = False
        while not self._stop.is_set():
            if self._pause.is_set():
                was_paused = True
                self._stop.wait(0.05)
                continue
            now = time.monotonic()
            if was_paused:  # 从暂停恢复：重新计时，避免瞬间触发
                was_paused = False
                next_tick = now + 1.0
            if now >= next_tick:
                self.remaining -= 1
                next_tick += 1.0  # 固定节拍，不随发送耗时漂移
                if next_tick < now:  # 落后过多（休眠/卡顿）时重新对齐
                    next_tick = now + 1.0
                if self.remaining <= 0:
                    self._fire()
                    if not self._stop.is_set():
                        self.fire_count += 1
                        self.remaining = self.total_sec
                        next_tick = time.monotonic() + 1.0
            else:
                self._stop.wait(min(0.05, next_tick - now))

    def to_dict(self):
        return {"type": "timer", "seq": list(self.seq),
                "minutes": self.minutes, "seconds": self.seconds, "gap": self.gap_ms}

    @classmethod
    def from_dict(cls, d):
        return cls(list(d.get("seq", ["a"])), int(d.get("minutes", 0)),
                   int(d.get("seconds", 10)), int(d.get("gap", 200)))


@dataclass
class Page:
    name: str = "配置"
    tasks: list = field(default_factory=list)

    def to_dict(self):
        return {"name": self.name, "tasks": [t.to_dict() for t in self.tasks]}

    @classmethod
    def from_dict(cls, d):
        tasks = []
        for td in d.get("tasks", []):
            try:
                if td.get("type") == "click":
                    tasks.append(ClickTask.from_dict(td))
                elif td.get("type") == "timer":
                    tasks.append(TimerTask.from_dict(td))
            except Exception:
                continue
        return cls(d.get("name", "配置"), tasks)


# ══════════════════════════════════════════════════════════
#  全局样式表
# ══════════════════════════════════════════════════════════
def make_qss() -> str:
    return f"""
    * {{ font-family: "{FONT_MAIN}"; font-size: 13px; color: {FG}; }}
    QMainWindow, QDialog, QWidget {{ background-color: {BG}; }}
    QLabel {{ background: transparent; }}
    #Card {{
        background-color: {BG_CARD};
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    #Title {{ font-size: 22px; font-weight: bold; color: {ACCENT}; }}
    #Sub   {{ font-size: 12px; color: {FG_DIM}; }}
    #CardTitle {{ font-size: 15px; font-weight: bold; }}
    QLineEdit {{
        background-color: {BG_ENTRY};
        color: {FG};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 6px 9px;
        selection-background-color: {ACCENT};
        selection-color: #11111b;
    }}
    QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
    QPushButton {{
        background-color: {BG_CARD};
        color: {FG};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 7px 14px;
    }}
    QPushButton:hover {{ border-color: {ACCENT}; background-color: {BG_HOVER}; }}
    QPushButton:pressed {{ background-color: #444a6a; }}
    QPushButton:disabled {{ color: {FG_DIM}; border-color: {BORDER}; background-color: {BG_ENTRY}; }}
    QPushButton#Primary {{
        background-color: {ACCENT}; color: #11111b; border: none; font-weight: bold;
    }}
    QPushButton#Primary:hover {{ background-color: #a0c0ff; }}
    QPushButton#Success {{
        background-color: {GREEN}; color: #11111b; border: none; font-weight: bold;
    }}
    QPushButton#Success:hover {{ background-color: #c0efbc; }}
    QPushButton#Danger {{
        background-color: {RED}; color: #11111b; border: none; font-weight: bold;
    }}
    QPushButton#Danger:hover {{ background-color: #f5a0ba; }}
    QPushButton#Warn {{
        background-color: {ORANGE}; color: #11111b; border: none; font-weight: bold;
    }}
    QPushButton#Warn:hover {{ background-color: #ffc49a; }}
    QPushButton#Ghost {{
        background-color: transparent; color: {FG_DIM}; border: 1px solid {BORDER};
    }}
    QPushButton#Ghost:hover {{ color: {FG}; border-color: {ACCENT}; }}
    QPushButton#Icon {{
        background: transparent; border: none; color: {FG_DIM};
        font-size: 15px; padding: 2px 8px;
    }}
    QPushButton#Icon:hover {{ color: {ACCENT}; }}
    QPushButton#IconDanger {{
        background: transparent; border: none; color: {FG_DIM};
        font-size: 15px; padding: 2px 8px;
    }}
    QPushButton#IconDanger:hover {{ color: {RED}; background: transparent; }}
    QPushButton#IconWarn {{
        background: transparent; border: none; color: {FG_DIM};
        font-size: 15px; padding: 2px 8px;
    }}
    QPushButton#IconWarn:hover {{ color: {ORANGE}; background: transparent; }}
    QComboBox {{
        background-color: {BG_ENTRY}; border: 1px solid {BORDER};
        border-radius: 6px; padding: 5px 10px;
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background-color: {BG_ENTRY}; border: 1px solid {BORDER};
        selection-background-color: {ACCENT}; selection-color: #11111b; outline: none;
    }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; }}
    QScrollBar::handle:vertical {{
        background: {BORDER}; border-radius: 4px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QStatusBar {{ background: {BG}; color: {FG_DIM}; }}
    QToolTip {{
        background-color: {BG_ENTRY}; color: {FG};
        border: 1px solid {BORDER}; padding: 4px;
    }}
    QDialogButtonBox QPushButton {{ min-width: 64px; }}
    """


# ══════════════════════════════════════════════════════════
#  小部件
# ══════════════════════════════════════════════════════════
def _label(text: str, style: str = "", fixed_w: int | None = None) -> QLabel:
    lb = QLabel(text)
    if style:
        lb.setStyleSheet(style)
    if fixed_w:
        lb.setFixedWidth(fixed_w)
    return lb


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)


# 录入期间需要让全局热键暂时失效：否则录入 F6 / Ctrl+` 这类按键时，
# 钩子会先把它们当成热键触发（F6 = 全部停止），按键就没法被录进去。
_recording_depth = 0


def is_recording() -> bool:
    """是否有录入器正在监听（主线程写、钩子线程读）。"""
    return _recording_depth > 0


class KeyRecorder(QObject):
    """全局按键录入器：keyboard 钩子线程 → 主线程信号。

    Windows 钩子给出的是**带方向**的修饰键名（left ctrl / left shift /
    left windows，见 keyboard 源码 _winkeyboard.py），直接判断
    ``name in ("ctrl", "shift", ...)`` 永远不成立，会把修饰键当成普通
    按键录入并丢掉组合关系。这里统一交给 keyboard.get_hotkey_name()
    归一化：既能去方向（left ctrl → ctrl），又能按标准顺序拼接组合键。
    """

    captured = pyqtSignal(str)   # 一个完整按键或组合键，如 "ctrl+f5"
    cancelled = pyqtSignal()     # 录入过程中按下 Esc

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hook = None
        self._held: set[str] = set()   # 仅钩子线程访问
        self.active = False

    def start(self) -> bool:
        """开始监听全局按键。返回 False 表示钩子注册失败（多为权限不足）。"""
        global _recording_depth
        if self.active:
            return True
        self._held.clear()
        try:
            self._hook = keyboard.hook(self._on_event)
        except Exception:
            self._hook = None
            self.active = False
            return False
        self.active = True
        _recording_depth += 1
        return True

    def stop(self):
        """停止监听并注销钩子（可重复调用）。"""
        global _recording_depth
        if self.active:
            _recording_depth = max(0, _recording_depth - 1)
        self.active = False
        self._held.clear()
        hook, self._hook = self._hook, None
        if hook is not None:
            try:
                keyboard.unhook(hook)
            except Exception:
                pass

    def _on_event(self, event):
        name = normalize_key(event.name)
        try:
            if event.event_type == "down":
                if name in self._held:
                    return True          # 长按自动重复，忽略
                self._held.add(name)
                if keyboard.is_modifier(event.name):
                    return True          # 修饰键单独按下不算一个按键
                if name == "esc":
                    self.cancelled.emit()
                    return True
                self.captured.emit(keyboard.get_hotkey_name(sorted(self._held)))
            else:
                self._held.discard(name)
        except Exception:
            pass
        return True


class KeyCaptureDialog(QDialog):
    """单键/组合键录入对话框：按下想用的按键即完成录入，Esc 取消。

    用于连点模式的按键录入；钩子不可用时提示手动输入。
    """

    def __init__(self, parent=None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("按键录入")
        self.setModal(True)
        self.setMinimumWidth(400)
        self.setStyleSheet(make_qss())
        self._value = (current or "").strip()
        self.recorder = KeyRecorder(self)
        self.recorder.captured.connect(self._on_captured)
        self.recorder.cancelled.connect(self.reject)
        self._build()
        self._start()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)
        outer.addWidget(_label(
            "请按下要使用的按键；可同时按住 Ctrl / Shift / Alt / Win 组成组合键。",
            f"color:{FG_DIM}; font-size:12px;"))
        self.value_lb = _label(
            self._value or "（等待按键…）",
            f"background:{BG_ENTRY}; color:{ACCENT}; border:1px solid {BORDER};"
            f"border-radius:6px; font-family:{FONT_MONO}; font-size:16px;")
        self.value_lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value_lb.setMinimumHeight(52)
        outer.addWidget(self.value_lb)
        self.hint_lb = _label("", f"color:{FG_DIM}; font-size:11px;")
        self.hint_lb.setWordWrap(True)
        outer.addWidget(self.hint_lb)

        row = QHBoxLayout()
        self.btn_again = QPushButton("重新录入")
        self.btn_again.setObjectName("Warn")
        self.btn_again.clicked.connect(self._start)
        row.addWidget(self.btn_again)
        row.addStretch(1)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("Primary")
        self.btn_ok.setAutoDefault(False)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_ok)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("Ghost")
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_cancel)
        outer.addLayout(row)

    def value(self) -> str:
        return self._value

    def _start(self):
        if self.recorder.start():
            self.hint_lb.setText("等待按键…（按 Esc 取消录入）")
            self._set_busy(True)
        else:
            self.hint_lb.setText("无法启动键盘钩子，请尝试以管理员身份运行；也可以直接手动填写按键名。")
            self._set_busy(False)

    def _set_busy(self, busy: bool):
        """录入期间按钮不接受焦点，避免空格/回车被当成按钮点击。"""
        policy = Qt.FocusPolicy.NoFocus if busy else Qt.FocusPolicy.StrongFocus
        for btn in (self.btn_again, self.btn_ok, self.btn_cancel):
            btn.setFocusPolicy(policy)
        self.setFocus()

    def _on_captured(self, combo: str):
        self._value = combo
        self.value_lb.setText(combo)
        self.recorder.stop()
        self._set_busy(False)
        self.hint_lb.setText("已录入；可点「重新录入」重录，或点「确定」应用。")

    def keyPressEvent(self, event):
        if self.recorder.active:   # 录入期间吞掉对话框自身的按键
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self):
        self.recorder.stop()
        super().reject()

    def accept(self):
        self.recorder.stop()
        super().accept()


class _MainBridge(QObject):
    """全局热键（keyboard 钩子线程）→ 主线程"""
    toggleAll = pyqtSignal()   # Ctrl+` 开始/停止当前页
    panicAll = pyqtSignal()    # F6 全部停止（紧急停止）


class TaskRow(QFrame):
    """单条任务行"""
    playReq = pyqtSignal(object)
    stopReq = pyqtSignal(object)
    delReq  = pyqtSignal(object)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        self.setObjectName("Card")
        self.setFixedHeight(52)
        self._build()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 5, 12, 5)
        lay.setSpacing(8)

        if isinstance(self.task, ClickTask):
            tag, tag_c = "连点", GREEN
        else:
            tag, tag_c = "定时", ORANGE
        lay.addWidget(_label(tag, f"color:{tag_c}; font-weight:bold;", 38))

        if isinstance(self.task, ClickTask):
            key_text = self.task.keys
            info_text = f"间隔 {self.task.interval_ms} ms"
            self.prog_lb = _label("0", f"color:{FG_DIM}; font-family:{FONT_MONO};", 52)
        else:
            key_text = "   ·   ".join(self.task.seq) or "(空)"
            m, s = divmod(self.task.total_sec, 60)
            info_text = f"{m:02d}:{s:02d}  间隔 {self.task.gap_ms} ms"
            self.prog_lb = _label(f"{m:02d}:{s:02d}",
                                  f"color:{FG_DIM}; font-family:{FONT_MONO};", 52)

        left = QVBoxLayout()
        left.setSpacing(0)
        self.key_lb = _label(key_text, f"color:{ACCENT}; font-family:{FONT_MONO}; font-size:12px;")
        self.info_lb = _label(info_text, f"color:{FG_DIM}; font-size:11px;")
        left.addWidget(self.key_lb)
        left.addWidget(self.info_lb)
        lay.addLayout(left, 1)

        self.status_lb = _label("○ 停止", f"color:{FG_DIM};", 52)
        lay.addWidget(self.status_lb)
        lay.addWidget(self.prog_lb)

        self.btn_play = QPushButton("▶")
        self.btn_play.setObjectName("Icon")
        self.btn_play.setFixedWidth(36)
        self.btn_play.clicked.connect(lambda: self.playReq.emit(self.task))
        self.btn_stop = QPushButton("■")
        self.btn_stop.setObjectName("IconDanger")
        self.btn_stop.setFixedWidth(36)
        self.btn_stop.clicked.connect(lambda: self.stopReq.emit(self.task))
        self.btn_del = QPushButton("✕")
        self.btn_del.setObjectName("IconDanger")
        self.btn_del.setFixedWidth(36)
        self.btn_del.clicked.connect(lambda: self.delReq.emit(self.task))
        lay.addWidget(self.btn_play)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.btn_del)

    def refresh(self):
        t = self.task
        running, paused = t.running, t.paused
        if running and not paused:
            self.status_lb.setText("● 运行")
            self.status_lb.setStyleSheet(f"color:{GREEN};")
            self.btn_play.setVisible(True)
            self.btn_stop.setVisible(True)
        elif paused:
            self.status_lb.setText("● 暂停")
            self.status_lb.setStyleSheet(f"color:{ORANGE};")
            self.btn_play.setVisible(True)
            self.btn_stop.setVisible(True)
        else:
            self.status_lb.setText("○ 停止")
            self.status_lb.setStyleSheet(f"color:{FG_DIM};")
            self.btn_play.setVisible(True)
            self.btn_stop.setVisible(False)

        if isinstance(t, ClickTask):
            self.prog_lb.setText(str(t.click_count))
            self.prog_lb.setStyleSheet(
                f"color:{YELLOW}; font-family:{FONT_MONO};"
                if running and not paused
                else f"color:{FG_DIM}; font-family:{FONT_MONO};")
        else:
            m, s = divmod(max(t.remaining, 0), 60)
            self.prog_lb.setText(f"{m:02d}:{s:02d}")
            self.prog_lb.setStyleSheet(
                f"color:{GREEN}; font-family:{FONT_MONO};"
                if running and not paused
                else f"color:{FG_DIM}; font-family:{FONT_MONO};")

    def is_running(self):
        return self.task.running or self.task.paused


class SequenceEditor(QDialog):
    """录入 / 编辑按键序列"""

    def __init__(self, parent=None, existing=None):
        super().__init__(parent)
        self.setWindowTitle("按键序列")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setStyleSheet(make_qss())
        self._seq = [format_keys(parse_keys(str(s))) or str(s) for s in (existing or [])]
        self._recording = False
        self.recorder = KeyRecorder(self)
        self.recorder.captured.connect(self._append_key)
        self.recorder.cancelled.connect(self._finish_recording)
        self._build()
        self._render()
        self._stop_recording()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(10)
        outer.addWidget(_label("按键序列（录制时按 Esc 结束）：", f"color:{FG_DIM}; font-size:12px;"))

        self.seq_lb = _label("", f"background:{BG_ENTRY}; color:{PURPLE}; padding:10px 12px;"
                              f"border:1px solid {BORDER}; border-radius:6px;"
                              f"font-family:{FONT_MONO}; font-size:12px;")
        self.seq_lb.setMinimumHeight(46)
        self.seq_lb.setWordWrap(True)
        outer.addWidget(self.seq_lb)

        self.rec_btn = QPushButton("● 开始录入")
        self.rec_btn.setObjectName("Warn")
        self.rec_btn.clicked.connect(self._toggle_recording)
        outer.addWidget(self.rec_btn)

        outer.addWidget(_label("或手动输入（逗号分隔，支持组合键 如  ctrl+shift+f5）：",
                               f"color:{FG_DIM}; font-size:11px;"))
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("例: a, ctrl+f9, space, enter")
        outer.addWidget(self.manual_edit)

        row = QHBoxLayout()
        b_append = QPushButton("追加到序列")
        b_append.clicked.connect(self._append_manual)
        row.addWidget(b_append)
        b_clear = QPushButton("清空")
        b_clear.setObjectName("Ghost")
        b_clear.clicked.connect(self._clear_seq)
        row.addWidget(b_clear)
        row.addStretch(1)
        outer.addLayout(row)

        bb = QHBoxLayout()
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("Primary")
        self.btn_ok.setAutoDefault(False)
        self.btn_ok.clicked.connect(self._on_ok)
        bb.addWidget(self.btn_ok)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("Ghost")
        self.btn_cancel.setAutoDefault(False)
        self.btn_cancel.clicked.connect(self.reject)
        bb.addWidget(self.btn_cancel)
        bb.addStretch(1)
        outer.addLayout(bb)

    def seq(self):
        return list(self._seq)

    def _render(self):
        txt = "   →   ".join(self._seq) if self._seq else "(空)"
        self.seq_lb.setText(txt)

    def _toggle_recording(self):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        self._recording = True
        self._seq.clear()
        self._render()
        self.rec_btn.setText("■ 停止录入")
        self.rec_btn.setObjectName("Danger")
        self.rec_btn.setStyleSheet(make_qss())
        # 录制期间：按钮/输入框不接收焦点，避免空格/回车误触
        self.rec_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.manual_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFocus()
        if not self.recorder.start():
            self._stop_recording()
            QMessageBox.warning(self, "无法录入",
                                "启动键盘钩子失败。\n"
                                "请尝试以管理员身份运行本程序；\n"
                                "也可在下方手动输入按键名。")

    def keyPressEvent(self, e):
        if self._recording:
            if e.key() == Qt.Key.Key_Escape:
                self._stop_recording()
                self._render()
                e.accept()
                return
            e.accept()
            return
        super().keyPressEvent(e)

    def _finish_recording(self):
        if self._recording:
            self._stop_recording()
            self._render()

    def _append_key(self, combo):
        """KeyRecorder 录入到一个按键/组合键（已归一化）。"""
        if not self._recording:
            return
        self._seq.append(combo)
        self.seq_lb.setText("录制中  " + "   →   ".join(self._seq))

    def _stop_recording(self):
        self._recording = False
        self.recorder.stop()
        self.rec_btn.setText("● 开始录入")
        self.rec_btn.setObjectName("Warn")
        self.rec_btn.setStyleSheet(make_qss())
        self.rec_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.manual_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._render()

    def _append_manual(self):
        raw = self.manual_edit.text().strip()
        if not raw:
            return
        bad = []
        for part in raw.split(","):
            s = part.strip()
            if not s:
                continue
            keys, err = validate_keys(s)
            if err:
                bad.append(f"  · {s}（{err}）")
                continue
            self._seq.append(format_keys(keys) or s)
        self.manual_edit.clear()
        self._render()
        if bad:
            QMessageBox.warning(self, "按键名有误",
                                "以下内容未加入序列：\n" + "\n".join(bad))

    def _clear_seq(self):
        self._seq.clear()
        self._render()

    def _on_ok(self):
        if not self._seq:
            QMessageBox.warning(self, "提示", "按键序列不能为空")
            return
        self._stop_recording()
        self.accept()

    def reject(self):
        self._stop_recording()
        super().reject()


# ══════════════════════════════════════════════════════════
#  配置页面板
# ══════════════════════════════════════════════════════════
class PagePanel(QWidget):
    dataChanged = pyqtSignal()

    def __init__(self, page: Page, parent=None):
        super().__init__(parent)
        self.page = page
        self.rows: list[TaskRow] = []
        self._timer_seq: list = []
        self._build()
        self._rebuild_rows()
        self._refresh_stats()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # ── 添加任务卡片 ──
        add_card = Card()
        al = QVBoxLayout(add_card)
        al.setContentsMargins(16, 12, 16, 14)
        al.setSpacing(10)

        mode_row = QHBoxLayout()
        mode_row.addWidget(_label("任务类型"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["〇  连点", "〇  定时序列"])
        self.mode_combo.setFixedWidth(150)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        al.addLayout(mode_row)

        # 连点参数
        self.click_grp = QWidget()
        cg = QHBoxLayout(self.click_grp)
        cg.setContentsMargins(0, 0, 0, 0)
        cg.setSpacing(8)
        cg.addWidget(_label("按键"))
        self.click_key = QLineEdit("a")
        self.click_key.setFixedWidth(110)
        self.click_key.setPlaceholderText("如 a 或 ctrl+f9")
        cg.addWidget(self.click_key)
        self.click_rec_btn = QPushButton("● 录入")
        self.click_rec_btn.setObjectName("Warn")
        self.click_rec_btn.setFixedWidth(72)
        self.click_rec_btn.setToolTip("按下要连点的按键（支持组合键），无需手动输入")
        self.click_rec_btn.clicked.connect(self._record_click_key)
        cg.addWidget(self.click_rec_btn)
        cg.addWidget(_label("间隔"))
        self.click_interval = QLineEdit("100")
        self.click_interval.setFixedWidth(60)
        self.click_interval.setValidator(QIntValidator(MIN_INTERVAL_MS, 3600000, self))
        cg.addWidget(self.click_interval)
        cg.addWidget(_label("ms"))
        cg.addStretch(1)
        al.addWidget(self.click_grp)

        # 定时序列参数
        self.timer_grp = QWidget()
        tv = QVBoxLayout(self.timer_grp)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(4)
        r1 = QHBoxLayout()
        r1.addWidget(_label("倒计时"))
        self.tmin_edit = QLineEdit("0")
        self.tmin_edit.setFixedWidth(48)
        self.tmin_edit.setValidator(QIntValidator(0, 999, self))
        r1.addWidget(self.tmin_edit)
        r1.addWidget(_label("分"))
        self.tsec_edit = QLineEdit("10")
        self.tsec_edit.setFixedWidth(48)
        self.tsec_edit.setValidator(QIntValidator(0, 59, self))
        r1.addWidget(self.tsec_edit)
        r1.addWidget(_label("秒"))
        r1.addWidget(_label("   按键间隔"))
        self.gap_edit = QLineEdit("200")
        self.gap_edit.setFixedWidth(56)
        self.gap_edit.setValidator(QIntValidator(16, 60000, self))
        r1.addWidget(self.gap_edit)
        r1.addWidget(_label("ms"))
        r1.addStretch(1)
        tv.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(_label("按键序列"))
        self.seq_btn = QPushButton("点击设置序列")
        self.seq_btn.setObjectName("Ghost")
        self.seq_btn.clicked.connect(self._edit_seq)
        r2.addWidget(self.seq_btn, 1)
        tv.addLayout(r2)

        self.timer_grp.hide()
        al.addWidget(self.timer_grp)

        add_btn = QPushButton("＋  添加到任务列表")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add_task)
        al.addWidget(add_btn)
        root.addWidget(add_card)

        # ── 任务列表卡片 ──
        list_card = Card()
        ll = QVBoxLayout(list_card)
        ll.setContentsMargins(12, 10, 12, 10)
        ll.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("任务列表")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.stat_lb = _label("", f"color:{FG_DIM}; font-size:11px;")
        head.addWidget(self.stat_lb)
        b_clear = QPushButton("清空")
        b_clear.setObjectName("Ghost")
        b_clear.clicked.connect(self._clear_all)
        head.addWidget(b_clear)
        ll.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.container = QWidget()
        self.container.setStyleSheet("background: transparent;")
        self.task_layout = QVBoxLayout(self.container)
        self.task_layout.setContentsMargins(0, 0, 4, 0)
        self.task_layout.setSpacing(6)
        self.empty_lb = _label("暂无任务，请在上方添加", f"color:{FG_DIM};")
        self.empty_lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.task_layout.addWidget(self.empty_lb)
        self.task_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        ll.addWidget(self.scroll, 1)
        root.addWidget(list_card, 1)

    def _mode_changed(self, i):
        self.click_grp.setVisible(i == 0)
        self.timer_grp.setVisible(i == 1)

    def _record_click_key(self):
        """打开按键录入对话框，把录入到的按键填回输入框。"""
        dlg = KeyCaptureDialog(self, self.click_key.text().strip())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        value = dlg.value().strip()
        if not value:
            QMessageBox.warning(self, "提示", "尚未录入任何按键")
            return
        keys, err = validate_keys(value)
        if err:
            QMessageBox.warning(self, "按键名有误", err)
            return
        self.click_key.setText(format_keys(keys) or value)

    def _edit_seq(self):
        dlg = SequenceEditor(self, self._timer_seq)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._timer_seq = dlg.seq()
            self._refresh_seq_btn()

    def _refresh_seq_btn(self):
        if self._timer_seq:
            self.seq_btn.setText("   →   ".join(self._timer_seq))
            self.seq_btn.setStyleSheet(
                f"QPushButton {{ background:{BG_ENTRY}; color:{PURPLE};"
                f"border:1px solid {BORDER}; border-radius:6px; padding:6px 10px;"
                f"font-family:{FONT_MONO}; font-size:12px; }}")
        else:
            self.seq_btn.setText("点击设置序列")
            self.seq_btn.setStyleSheet(
                f"QPushButton {{ background:transparent; color:{FG_DIM};"
                f"border:1px dashed {BORDER}; border-radius:6px; padding:6px 10px; }}")

    def _add_task(self):
        if self.mode_combo.currentIndex() == 0:
            keys, err = validate_keys(self.click_key.text())
            if err:
                QMessageBox.warning(self, "按键名有误", err)
                return
            try:
                ms = max(int(self.click_interval.text() or 100), MIN_INTERVAL_MS)
            except ValueError:
                QMessageBox.warning(self, "提示", "间隔必须为整数(毫秒)")
                return
            key_text = format_keys(keys) or self.click_key.text().strip()
            self.click_key.setText(key_text)
            task = ClickTask(key_text, ms)
        else:
            if not self._timer_seq:
                QMessageBox.warning(self, "提示", "请先设置按键序列")
                return
            try:
                m = int(self.tmin_edit.text() or 0)
                s = int(self.tsec_edit.text() or 0)
                gap = max(int(self.gap_edit.text() or 200), MIN_INTERVAL_MS)
            except ValueError:
                QMessageBox.warning(self, "提示", "时间/间隔必须为整数")
                return
            if m < 0 or s < 0:
                QMessageBox.warning(self, "提示", "时间不能为负数")
                return
            if m == 0 and s == 0:
                QMessageBox.warning(self, "提示", "倒计时不能为 0")
                return
            task = TimerTask(list(self._timer_seq), m, s, gap)
        self.page.tasks.append(task)
        self._rebuild_rows()
        self.dataChanged.emit()

    def _rebuild_rows(self):
        for r in self.rows:
            r.deleteLater()
        self.rows.clear()
        self.empty_lb.setVisible(len(self.page.tasks) == 0)
        for t in self.page.tasks:
            row = TaskRow(t)
            row.playReq.connect(self._on_play)
            row.stopReq.connect(self._on_stop)
            row.delReq.connect(self._on_delete)
            self.rows.append(row)
            self.task_layout.insertWidget(self.task_layout.count() - 1, row)
        self._refresh_stats()

    def _on_play(self, t):
        if t.running:
            t.toggle_pause()
        else:
            t.start()
        self._refresh_rows()
        self.dataChanged.emit()

    def _on_stop(self, t):
        t.stop()
        self._refresh_rows()
        self.dataChanged.emit()

    def _on_delete(self, t):
        t.stop()
        self.page.tasks.remove(t)
        self._rebuild_rows()
        self.dataChanged.emit()

    def start_all(self):
        for t in self.page.tasks:
            t.start()
        self._refresh_rows()

    def stop_all(self):
        for t in self.page.tasks:
            t.stop()
        self._refresh_rows()

    def _clear_all(self):
        for t in self.page.tasks:
            t.stop()
        self.page.tasks.clear()
        self._rebuild_rows()
        self.dataChanged.emit()

    def _refresh_rows(self):
        for r in self.rows:
            r.refresh()

    def _refresh_stats(self):
        n = len(self.page.tasks)
        run = sum(1 for t in self.page.tasks if t.running)
        self.stat_lb.setText(f"{n} 个任务  ·  {run} 运行中")

    def page_name(self):
        return self.page.name


# ══════════════════════════════════════════════════════════
#  主窗口
# ══════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(760, 560)
        self.resize(880, 660)
        self.pages: list[Page] = []
        self.panels: list[PagePanel] = []
        self.setStyleSheet(make_qss())
        self._build()
        self._load_config()
        if not self.pages:
            self.pages = [Page("配置1")]
        self._rebuild_stack()
        self.poller = QTimer(self)
        self.poller.timeout.connect(self._poll)
        self.poller.start(250)
        # 热键回调在 keyboard 钩子线程触发，必须经信号转回主线程再操作 UI
        self._bridge = _MainBridge()
        self._bridge.toggleAll.connect(self._hotkey_toggle)
        self._bridge.panicAll.connect(self._hotkey_panic)
        self._setup_hotkey()
        self._center()

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(20, 12, 20, 4)
        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        hl.addWidget(title)
        hl.addWidget(_label("   连点 · 定时序列 · 多配置页", f"color:{FG_DIM}; font-size:12px;"))
        hl.addStretch(1)
        root.addWidget(head)

        # 操作栏
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(20, 6, 20, 10)
        bl.setSpacing(8)
        bl.addWidget(_label("当前页"))

        self.page_combo = QComboBox()
        self.page_combo.setFixedWidth(140)
        self.page_combo.currentIndexChanged.connect(self._switch_page)
        bl.addWidget(self.page_combo)

        b_add = QPushButton("＋ 新配置页")
        b_add.setObjectName("Ghost")
        b_add.clicked.connect(self._add_page)
        bl.addWidget(b_add)
        b_rename = QPushButton("✎ 重命名")
        b_rename.setObjectName("Ghost")
        b_rename.clicked.connect(self._rename_page)
        bl.addWidget(b_rename)
        b_del = QPushButton("🗑 删除页")
        b_del.setObjectName("Ghost")
        b_del.clicked.connect(self._del_page)
        bl.addWidget(b_del)

        bl.addStretch(1)
        self.btn_start_all = QPushButton("▶▶ 全部开始")
        self.btn_start_all.setObjectName("Success")
        self.btn_start_all.clicked.connect(self._start_all)
        bl.addWidget(self.btn_start_all)
        self.btn_stop_all = QPushButton("■■ 全部停止")
        self.btn_stop_all.setObjectName("Danger")
        self.btn_stop_all.clicked.connect(self._stop_all)
        bl.addWidget(self.btn_stop_all)
        root.addWidget(bar)

        # 页面堆叠
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.statusBar()
        self.statusBar().showMessage(
            "就绪   ·   热键 Ctrl+` 开始/停止当前页，F6 全部停止   ·   配置自动保存")

    def _center(self):
        scr = QApplication.primaryScreen()
        if scr:
            geo = self.frameGeometry()
            geo.moveCenter(scr.availableGeometry().center())
            self.move(geo.topLeft())

    def _rebuild_stack(self):
        while self.stack.count():
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.deleteLater()
        self.panels.clear()
        for p in self.pages:
            panel = PagePanel(p)
            panel.dataChanged.connect(self._save)
            self.stack.addWidget(panel)
            self.panels.append(panel)
        self.page_combo.blockSignals(True)
        self.page_combo.clear()
        for p in self.pages:
            self.page_combo.addItem(p.name)
        self.page_combo.blockSignals(False)
        if self.pages:
            self.page_combo.setCurrentIndex(0)
            self.stack.setCurrentIndex(0)

    def _switch_page(self, i):
        if 0 <= i < self.stack.count():
            self.stack.setCurrentIndex(i)
            panel = self._current_panel()
            if panel is not None:
                panel._refresh_rows()
                panel._refresh_stats()

    def _add_page(self):
        p = Page(f"配置{len(self.pages) + 1}")
        self.pages.append(p)
        self._rebuild_stack()
        self.page_combo.setCurrentIndex(len(self.pages) - 1)
        self._save()

    def _rename_page(self):
        i = self.stack.currentIndex()
        if not (0 <= i < len(self.pages)):
            return
        name, ok = QInputDialog.getText(self, "重命名", "新名称:", text=self.pages[i].name)
        if ok and name.strip():
            self.pages[i].name = name.strip()
            self._rebuild_stack()
            self.page_combo.setCurrentIndex(i)  # 保持当前页不变
            self._save()

    def _del_page(self):
        if len(self.pages) <= 1:
            QMessageBox.information(self, "提示", "至少保留一个配置页")
            return
        i = self.stack.currentIndex()
        if not (0 <= i < len(self.pages)):
            return
        for t in self.pages[i].tasks:
            t.stop()
        del self.pages[i]
        self._rebuild_stack()
        # 删除后停留在原位置（最后一页则前移）
        self.page_combo.setCurrentIndex(max(min(i, len(self.pages) - 1), 0))
        self._save()

    def _current_panel(self):
        i = self.stack.currentIndex()
        if 0 <= i < len(self.panels):
            return self.panels[i]
        return None

    def _start_all(self):
        p = self._current_panel()
        if p:
            p.start_all()

    def _stop_all(self):
        p = self._current_panel()
        if p:
            p.stop_all()

    def _hotkey_toggle(self):
        if is_recording():
            return          # 录入按键时不响应全局热键
        p = self._current_panel()
        if p:
            if any(t.running for t in p.page.tasks):
                p.stop_all()
            else:
                p.start_all()

    def _setup_hotkey(self):
        hotkeys = []
        for combo, signal in (("ctrl+`", self._bridge.toggleAll),
                              ("f6", self._bridge.panicAll)):
            try:
                keyboard.add_hotkey(combo, signal.emit)
                hotkeys.append(combo)
            except Exception:
                continue
        if hotkeys:
            names = " / ".join("Ctrl+`" if c == "ctrl+`" else c.upper() for c in hotkeys)
            self.statusBar().showMessage(f"就绪   ·   热键 {names}   ·   配置自动保存")
        else:
            self.statusBar().showMessage(
                "就绪   ·   全局热键注册失败（可尝试以管理员身份运行）   ·   配置自动保存")

    def _hotkey_panic(self):
        """F6：停掉所有页面的全部任务（紧急停止）。"""
        if is_recording():
            return          # 正在录入按键（可能就是 F6）时不触发
        for page in self.pages:
            for t in page.tasks:
                t.stop()
        panel = self._current_panel()
        if panel is not None:
            panel._refresh_rows()
            panel._refresh_stats()
        self.statusBar().showMessage("已全部停止（F6）", 3000)

    def _poll(self):
        # 只刷新当前可见面板，避免后台页白白重绘
        panel = self._current_panel()
        if panel is not None:
            panel._refresh_rows()
            panel._refresh_stats()

    def _save(self):
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([p.to_dict() for p in self.pages], f,
                          ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)  # 原子替换，避免写一半损坏配置
        except Exception:
            pass

    def _load_config(self):
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.pages = [Page.from_dict(d) for d in data if isinstance(d, dict)]
        except Exception:
            pass

    def closeEvent(self, event):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        for p in self.pages:
            for t in p.tasks:
                t.stop()
        self._save()
        super().closeEvent(event)


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()