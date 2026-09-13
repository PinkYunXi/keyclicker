"""键盘连点器 —— PyQt6 重构版

功能：
  · 连点模式：按固定间隔持续发送指定按键，支持 ctrl+f9 这类组合键
  · 定时序列：倒计时结束后依次发送一串按键，可循环触发
  · 多配置页：多套互不干扰的任务，配置自动保存（原子写入）
  · 按键录入：连点按键与定时序列都能"按下即录入"，无需手写键名
  · 窗口锁定：仅当前台窗口标题匹配时才发送按键
  · 双主题：GitHub Primer 风格浅色 / 深色，一键切换并记忆
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
from string import Template

__version__ = "1.2.1"
__all__ = [
    "main", "parse_keys", "normalize_key", "is_valid_key", "validate_keys",
    "send_keys", "make_qss", "repolish", "current_theme", "set_current_theme",
    "system_theme", "resolve_config_path", "ensure_config_dir",
    "foreground_window_title", "foreground_process_name", "match_window",
    "list_window_titles", "app_icon", "install_focus_clearer",
    "ClickTask", "TimerTask", "Page", "KeyRecorder", "KeyCaptureDialog",
    "SequenceEditor", "TaskRow", "WindowCombo", "PagePanel", "MainWindow",
]

try:
    from PyQt6.QtCore import Qt, QEvent, QTimer, pyqtSignal, QObject
    from PyQt6.QtGui import QIcon, QIntValidator
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
        QComboBox, QScrollArea, QFrame, QVBoxLayout, QHBoxLayout, QDialog,
        QMessageBox, QSizePolicy, QStackedWidget, QInputDialog, QMenu,
        QAbstractItemView, QAbstractSpinBox,
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


# ══════════════════════════════════════════════════════════
#  主题（GitHub Primer 色板）
# ══════════════════════════════════════════════════════════
# 所有颜色都集中在这里，界面控件一律不写死颜色，只设置动态属性，
# 由 make_qss() 生成的样式表按属性选择器上色 —— 这样切换主题时
# 只要重新 setStyleSheet 一次，整个窗口立刻换肤。
THEMES = {
    "light": {
        "canvas": "#ffffff",
        "canvas_subtle": "#f6f8fa",
        "canvas_inset": "#f6f8fa",
        "border": "#d0d7de",
        "border_muted": "#d8dee4",
        "fg": "#1f2328",
        "fg_muted": "#59636e",
        "fg_subtle": "#818b98",
        "accent": "#0969da",
        "success": "#1a7f37",
        "success_emphasis": "#1f883d",
        "danger": "#cf222e",
        "danger_emphasis": "#cf222e",
        "attention": "#9a6700",
        "done": "#8250df",
        "btn_bg": "#f6f8fa",
        "btn_hover": "#eef1f4",
        "btn_active": "#e7ebef",
        "btn_fg": "#24292f",
        "on_emphasis": "#ffffff",
        # 序列 / 定时参数面板底色：浅色下用淡蓝底，与白色卡片明显区分
        "seq_bg": "#ddf4ff",
        "seq_border": "#b6e3ff",
        "seq_fg": "#0550ae",
        "field_bg": "#ffffff",
        "field_hover": "#eef1f4",
        "tooltip_bg": "#1f2328",
        "tooltip_fg": "#ffffff",
    },
    "dark": {
        "canvas": "#0d1117",
        "canvas_subtle": "#161b22",
        "canvas_inset": "#010409",
        "border": "#30363d",
        "border_muted": "#21262d",
        "fg": "#e6edf3",
        "fg_muted": "#8b949e",
        "fg_subtle": "#6e7681",
        "accent": "#2f81f7",
        "success": "#3fb950",
        "success_emphasis": "#238636",
        "danger": "#f85149",
        "danger_emphasis": "#da3633",
        "attention": "#d29922",
        "done": "#a371f7",
        "btn_bg": "#21262d",
        "btn_hover": "#30363d",
        "btn_active": "#282e33",
        "btn_fg": "#c9d1d9",
        "on_emphasis": "#ffffff",
        # 序列 / 定时参数面板底色：深色下用蓝黑底，与 #161b22 卡片明显区分
        "seq_bg": "#132339",
        "seq_border": "#1f4b7a",
        "seq_fg": "#79c0ff",
        "field_bg": "#0d1117",
        "field_hover": "#30363d",
        "tooltip_bg": "#161b22",
        "tooltip_fg": "#e6edf3",
    },
}

THEME_NAMES = ("light", "dark")
DEFAULT_THEME = "dark"
THEME_LABELS = {"light": "浅色", "dark": "深色"}

FONT_MAIN = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

APP_NAME = "键盘连点器"
CONFIG_VERSION = 2

# 当前主题（模块级，make_qss() 不带参数时用它）
_current_theme = DEFAULT_THEME


def normalize_theme(name) -> str:
    """把任意输入归一化为合法主题名；无法识别时回退到当前主题。"""
    if isinstance(name, str):
        key = name.strip().lower()
        if key in THEMES:
            return key
    return _current_theme


def current_theme() -> str:
    """当前主题名（"light" / "dark"）。"""
    return _current_theme


def set_current_theme(name: str) -> str:
    """设置当前主题；非法名字保持不变。返回生效后的主题名。"""
    global _current_theme
    if isinstance(name, str) and name.strip().lower() in THEMES:
        _current_theme = name.strip().lower()
    return _current_theme


def system_theme() -> str:
    """读取系统"应用模式"设置，返回 "light" / "dark"；读取失败默认深色。

    只读注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
    的 AppsUseLightTheme（1 = 浅色，0 = 深色）。winreg 延迟导入，非 Windows 直接回退。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if int(value) == 1 else "dark"
    except Exception:
        return DEFAULT_THEME


def repolish(widget) -> None:
    """动态属性改变后重新计算样式（含子控件），使属性选择器立即生效。"""
    try:
        targets = [widget]
        try:
            targets.extend(widget.findChildren(QWidget))
        except Exception:
            pass
        for w in targets:
            st = w.style()
            if st is None:
                continue
            st.unpolish(w)
            st.polish(w)
        widget.update()
    except Exception:
        pass


class _FocusClearer(QObject):
    """点窗口空白处时，让输入框失去焦点。

    不这么做的话，QSS 的聚焦边框（以及编辑框光标）会一直挂在最后一个
    输入框上，看上去像"多出来一个框"。装在 QApplication 上做全局过滤：
    鼠标按下时，如果落点不是输入类控件，就把同一窗口内当前聚焦的输入框
    clearFocus()。
    """

    _INPUTS = (QLineEdit, QComboBox, QAbstractSpinBox, QAbstractItemView)

    @classmethod
    def _is_input(cls, widget) -> bool:
        node = widget
        for _ in range(6):
            if node is None:
                return False
            if isinstance(node, cls._INPUTS):
                return True
            try:
                node = node.parent()
            except Exception:
                return False
        return False

    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(obj, QWidget):
                if not self._is_input(obj):
                    focus = QApplication.focusWidget()
                    if (focus is not None and self._is_input(focus)
                            and focus.window() is obj.window()):
                        focus.clearFocus()
        except Exception:
            pass
        return False


_focus_clearer = None


def install_focus_clearer(app=None) -> bool:
    """给应用安装"点空白处失焦"过滤器；重复调用不会重复安装。"""
    global _focus_clearer
    if app is None:
        app = QApplication.instance()
    if app is None:
        return False
    try:
        if _focus_clearer is not None and _focus_clearer.parent() is app:
            return True
        _focus_clearer = _FocusClearer(app)
        app.installEventFilter(_focus_clearer)
        return True
    except Exception:
        return False


def _resource_path(name: str) -> str:
    """随程序分发的资源文件路径（源码运行取脚本目录，exe 取解包目录）。"""
    base = getattr(sys, "_MEIPASS", None) or _app_dir()
    return os.path.join(base, name)


def app_icon() -> QIcon:
    """程序图标（logo.png）。找不到文件时返回空图标，不影响运行。"""
    for name in ("logo.png", os.path.join("docs", "logo.png")):
        try:
            path = _resource_path(name)
            if os.path.exists(path):
                icon = QIcon(path)
                if not icon.isNull():
                    return icon
        except Exception:
            continue
    return QIcon()


# 样式表模板：用 $token 占位，避免 Qt 的 {} 与 f-string 冲突
_QSS_TEMPLATE = """
* { font-family: "$font_main"; font-size: 13px; color: $fg; }
QMainWindow, QDialog, QWidget { background-color: $canvas; }
QLabel { background: transparent; }

/* ── 卡片 / 分隔线 ── */
QFrame#Card {
    background-color: $canvas_subtle;
    border: 1px solid $border;
    border-radius: 8px;
}
QFrame#HeaderSep {
    background-color: $border_muted;
    border: none;
    min-height: 1px;
    max-height: 1px;
}
QWidget#TaskContainer { background: transparent; }

/* ── 文本角色 ── */
QLabel[role="title"] { font-size: 20px; font-weight: 600; color: $fg; }
QLabel[role="subtitle"] { font-size: 12px; color: $fg_muted; }
QLabel[role="muted"] { color: $fg_muted; }
QLabel[role="subtle"] { color: $fg_subtle; font-size: 11px; }
QLabel[role="hint"] { color: $fg_muted; font-size: 11px; }
QLabel[role="cardTitle"] { font-size: 14px; font-weight: 600; color: $fg; }
QLabel[role="accent"] { color: $accent; }
QLabel[role="key"] { color: $accent; font-family: "$font_mono"; font-size: 12px; }
QLabel[role="tagClick"] { color: $success; font-weight: 600; }
QLabel[role="tagTimer"] { color: $attention; font-weight: 600; }
QLabel[role="window"] { color: $fg_muted; font-size: 11px; }
QLabel[role="warn"] { color: $attention; font-size: 11px; }
QLabel[role="status"] { color: $fg_subtle; }
QLabel[role="progress"] { color: $fg_subtle; }
QLabel[role="capture"] {
    background-color: $canvas_inset; color: $accent; border: 1px solid $border;
    border-radius: 6px; font-size: 16px; padding: 6px;
}
/* 按键序列显示框：浅色/深色下都换成强调色底，和周围明显拉开 */
QLabel[role="seqbox"] {
    background-color: $seq_bg; color: $seq_fg; border: 1px solid $seq_border;
    border-radius: 6px; font-size: 13px; padding: 10px 12px;
}

/* ── 任务行：状态由动态属性 state 驱动 ── */
QFrame#TaskRow {
    background-color: $canvas_subtle;
    border: 1px solid $border_muted;
    border-radius: 6px;
}
QFrame#TaskRow[state="running"] { border-color: $success_emphasis; }
QFrame#TaskRow[state="paused"] { border-color: $attention; }
QFrame#TaskRow[state="blocked"] { border-color: $attention; }
QFrame#TaskRow[state="running"] QLabel[role="status"] { color: $success; }
QFrame#TaskRow[state="paused"] QLabel[role="status"] { color: $attention; }
QFrame#TaskRow[state="blocked"] QLabel[role="status"] { color: $attention; }
QFrame#TaskRow[state="running"] QLabel[role="progress"] { color: $fg; }
QFrame#TaskRow[state="blocked"] QLabel[role="progress"] { color: $attention; }

/* ── 输入控件 ── */
QLineEdit {
    background-color: $canvas; color: $fg; border: 1px solid $border;
    border-radius: 6px; padding: 5px 8px;
    selection-background-color: $accent; selection-color: $on_emphasis;
}
QLineEdit:focus { border: 1px solid $accent; }
QLineEdit:disabled { background-color: $canvas_subtle; color: $fg_subtle; }
QComboBox {
    background-color: $field_bg; color: $fg; border: 1px solid $border;
    border-radius: 6px; padding: 5px 8px; outline: none;
}
/* 鼠标悬停：底色与边框同时反馈，浅色 / 深色下都清晰可见 */
QComboBox:hover { background-color: $field_hover; border: 1px solid $accent; }
QComboBox:focus, QComboBox:on {
    background-color: $field_bg; border: 1px solid $accent; outline: none;
}
QComboBox:disabled { color: $fg_subtle; border-color: $border_muted; }
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 22px; border: none; background: transparent;
}
/* 可编辑下拉里内嵌的输入框：清掉它自己的边框，避免出现“框中框” */
QComboBox QLineEdit {
    background: transparent; border: none; padding: 0; margin: 0; outline: none;
    selection-background-color: $accent; selection-color: $on_emphasis;
}
QComboBox QAbstractItemView {
    background-color: $field_bg; color: $fg; border: 1px solid $border;
    border-radius: 6px; padding: 4px; outline: none;
    selection-background-color: $accent; selection-color: $on_emphasis;
}
QComboBox QAbstractItemView::item { padding: 5px 8px; border-radius: 4px; }
QComboBox QAbstractItemView::item:hover { background-color: $field_hover; color: $fg; }

/* ── 按钮 ── */
QPushButton {
    background-color: $btn_bg; color: $btn_fg; border: 1px solid $border;
    border-radius: 6px; padding: 5px 12px; font-weight: 600;
}
QPushButton:hover { background-color: $btn_hover; }
QPushButton:pressed { background-color: $btn_active; }
QPushButton:disabled { color: $fg_subtle; border-color: $border_muted; }
QPushButton#Primary, QPushButton#Success {
    background-color: $success_emphasis; color: $on_emphasis;
    border: 1px solid $success_emphasis;
}
QPushButton#Primary:hover, QPushButton#Success:hover {
    background-color: $success; border-color: $success;
}
QPushButton#Primary:pressed, QPushButton#Success:pressed {
    background-color: $success_emphasis; border-color: $success_emphasis;
}
QPushButton#Danger {
    background-color: $danger_emphasis; color: $on_emphasis;
    border: 1px solid $danger_emphasis;
}
QPushButton#Danger:hover { background-color: $danger; border-color: $danger; }
QPushButton#Warn {
    background-color: $attention; color: $on_emphasis; border: 1px solid $attention;
}
QPushButton#Warn:hover { border-color: $fg; }
QPushButton#Primary:disabled, QPushButton#Success:disabled,
QPushButton#Danger:disabled, QPushButton#Warn:disabled {
    background-color: $btn_bg; color: $fg_subtle; border-color: $border_muted;
}
QPushButton#Ghost {
    background-color: transparent; color: $fg_muted; border: 1px solid $border;
}
QPushButton#Ghost:hover { background-color: $btn_hover; color: $fg; }
QPushButton#Icon, QPushButton#IconDanger {
    background: transparent; border: 1px solid transparent; border-radius: 6px;
    padding: 2px 8px; font-size: 14px; font-weight: 400; color: $fg_muted;
}
QPushButton#Icon:hover { background-color: $btn_hover; color: $accent; }
QPushButton#IconDanger:hover { background-color: $btn_hover; color: $danger; }
QPushButton#SeqBtn {
    font-family: "$font_main"; font-size: 13px; font-weight: 600;
    text-align: left; padding: 6px 10px;
}
QPushButton#SeqBtn[filled="true"] {
    background-color: $field_bg; color: $seq_fg; border: 1px solid $seq_border;
}
QPushButton#SeqBtn[filled="true"]:hover {
    background-color: $field_hover; color: $seq_fg; border-color: $seq_fg;
}
QPushButton#SeqBtn[filled="false"] {
    background-color: transparent; color: $seq_fg; border: 1px dashed $seq_border;
}
QPushButton#SeqBtn[filled="false"]:hover {
    background-color: $field_bg; color: $seq_fg; border-color: $seq_fg;
}
/* 定时序列参数面板：用强调色底把「倒计时 + 按键序列」整块托起来 */
QWidget#TimerGroup {
    background-color: $seq_bg; border: 1px solid $seq_border; border-radius: 8px;
}
QWidget#TimerGroup QLabel { color: $seq_fg; }

/* ── 菜单 ── */
QMenu {
    background-color: $canvas; color: $fg; border: 1px solid $border;
    border-radius: 6px; padding: 4px;
}
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 4px; }
QMenu::item:selected { background-color: $accent; color: $on_emphasis; }
QMenu::separator { height: 1px; background: $border_muted; margin: 4px 6px; }

/* ── 滚动条 / 状态栏 / 提示 ── */
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $border; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: $fg_subtle; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QStatusBar {
    background-color: $canvas_subtle; color: $fg_muted;
    border-top: 1px solid $border_muted;
}
QStatusBar::item { border: none; }
QToolTip {
    background-color: $tooltip_bg; color: $tooltip_fg;
    border: 1px solid $border; padding: 4px 6px; border-radius: 4px;
}
QDialogButtonBox QPushButton { min-width: 64px; }
"""


def make_qss(theme_name: str | None = None) -> str:
    """生成样式表。theme_name 省略或非法时使用当前主题。"""
    tokens = dict(THEMES[normalize_theme(theme_name)])
    tokens["font_main"] = FONT_MAIN
    tokens["font_mono"] = FONT_MONO
    return Template(_QSS_TEMPLATE).substitute(tokens)


# ── 配置位置 ──
def _app_dir() -> str:
    """程序目录：打包后是 exe 所在目录，否则是脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _user_config_path() -> str:
    """默认（非便携）配置路径：%APPDATA%\\KeyClicker\\config.json"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "KeyClicker", "config.json")


def resolve_config_path(app_dir: str | None = None) -> str:
    """解析配置文件路径，优先级由高到低：

    1. 环境变量 ``KEYCLICKER_CONFIG``（非空）
    2. 程序目录下**已存在**的 ``clicker_config.json``（兼容旧版便携用法）
    3. 程序目录下存在 ``portable.flag`` → 程序目录下 ``clicker_config.json``
    4. 否则 ``%APPDATA%\\KeyClicker\\config.json``

    这里只解析路径、不创建任何目录/文件（保持无副作用，方便测试与查询）；
    目录在真正写入配置时由 :func:`ensure_config_dir` 创建。
    """
    env = (os.environ.get("KEYCLICKER_CONFIG") or "").strip()
    if env:
        return env
    base = app_dir or _app_dir()
    portable = os.path.join(base, "clicker_config.json")
    if os.path.exists(portable):
        return portable
    if os.path.exists(os.path.join(base, "portable.flag")):
        return portable
    return _user_config_path()


def ensure_config_dir(path: str | None = None) -> str:
    """确保配置文件所在目录存在，返回该目录；任何失败都静默忽略。"""
    target = path or CONFIG_PATH
    d = os.path.dirname(os.path.abspath(target))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


# 模块级配置路径：测试会直接替换这个变量
CONFIG_PATH = resolve_config_path()

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
#  前台窗口标题匹配
# ══════════════════════════════════════════════════════════
# 窗口匹配模式：包含 / 精确 / 正则
WINDOW_MODES = ("contains", "exact", "regex")
WINDOW_MODE_LABELS = {"contains": "包含", "exact": "精确", "regex": "正则"}


def foreground_window_title() -> str:
    """当前前台窗口标题；失败或非 Windows 返回空串。

    每次只做一次轻量 Win32 调用，不起线程、不做缓存。
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception:
        return ""


def foreground_process_name() -> str:
    """前台窗口所属进程的可执行文件名（仅用于界面提示）；失败返回空串。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size))
            if not ok:
                return ""
            return os.path.basename(buf.value or "")
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


# 枚举窗口时忽略的系统噪声标题（小写比较）
_WINDOW_TITLE_SKIP = {
    "program manager",
    "windows input experience",
    "microsoft text input application",
    "windows 输入体验",
    "default ime",
    "msctfime ui",
}


def list_window_titles(limit: int = 40) -> list[str]:
    """列出当前所有"可见且有标题"的顶层窗口标题（去重、按标题排序）。

    供「窗口匹配」下拉框使用：只读操作，任何异常都返回空列表，
    绝不抛错。按 z 序枚举，所以最前面的通常是当前前台窗口。
    会跳过本程序自己的窗口与系统输入法之类的噪声窗口。
    """
    titles: list[str] = []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        own_pid = os.getpid()
        try:
            dwmapi = ctypes.windll.dwmapi
        except Exception:
            dwmapi = None
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _visit(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                # 跳过 UWP 隐藏窗口（被 DWM 遮盖的"幽灵窗口"）
                if dwmapi is not None:
                    cloaked = wintypes.DWORD(0)
                    dwmapi.DwmGetWindowAttribute(
                        hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                    if cloaked.value:
                        return True
                length = int(user32.GetWindowTextLengthW(hwnd))
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = (buf.value or "").strip()
                if not title or title.lower() in _WINDOW_TITLE_SKIP:
                    return True
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == own_pid:
                    return True
                titles.append(title)
            except Exception:
                pass
            return True

        user32.EnumWindows(enum_proc(_visit), 0)
    except Exception:
        return []

    seen = set()
    unique: list[str] = []
    for t in titles:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)
    unique.sort(key=lambda s: s.lower())
    return unique[:max(1, int(limit))]


def match_window(pattern: str, title: str, mode: str = "contains") -> bool:
    """纯函数：判断窗口标题是否命中匹配条件。

    · ``pattern`` 为空 → 恒为 True（即不限制窗口）
    · ``contains`` / ``exact`` 大小写不敏感
    · ``regex`` 使用 ``re.search``；非法正则返回 False 且不抛异常
    · 未知 mode 按 ``contains`` 处理
    """
    pat = (pattern or "").strip()
    if not pat:
        return True
    text = title or ""
    how = (mode or "contains").strip().lower()
    try:
        if how == "exact":
            return text.strip().lower() == pat.lower()
        if how == "regex":
            return re.search(pat, text, re.IGNORECASE) is not None
        return pat.lower() in text.lower()
    except re.error:
        return False          # 正则写错 → 视为不匹配，绝不抛出
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
#  任务模型（后台线程）
# ══════════════════════════════════════════════════════════
class _BaseTask:
    def __init__(self):
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None
        self._window_blocked = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def window_blocked(self) -> bool:
        """只读：当前是否因为前台窗口不匹配而停止发送按键。"""
        return bool(getattr(self, "_window_blocked", False))

    def stop(self):
        self._stop.set()
        self._pause.clear()
        if self._thread:
            self._thread.join(timeout=0.8)
        self._thread = None
        self._window_blocked = False

    def toggle_pause(self):
        if self._pause.is_set():
            self._pause.clear()
        else:
            self._pause.set()

    def _window_allows(self) -> bool:
        """前台窗口是否满足本任务的窗口匹配条件。

        未设置匹配条件时直接返回 True，连 Win32 调用都省掉。
        """
        pattern = getattr(self, "window_match", "") or ""
        if not pattern.strip():
            return True
        return match_window(pattern, foreground_window_title(),
                            getattr(self, "window_mode", "contains"))

    def _launch(self, target):
        # 每次启动使用全新的事件对象，避免旧线程未退出时被"复活"
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._window_blocked = False
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()


@dataclass
class ClickTask(_BaseTask):
    """连点任务：每隔 interval_ms 发送一次按键（keys 支持 ctrl+f9 组合键）。"""

    keys: str = "a"
    interval_ms: int = 100
    click_count: int = 0
    # ── v1.2.0 新增（追加在末尾，保持原有位置参数顺序不变）──
    window_match: str = ""
    window_mode: str = "contains"

    def __post_init__(self):
        _BaseTask.__init__(self)
        self.keys = "+".join(parse_keys(self.keys)) or "a"
        self.interval_ms = max(int(self.interval_ms), MIN_INTERVAL_MS)
        self._norm_window()

    def _norm_window(self):
        self.window_match = str(self.window_match or "").strip()
        mode = str(self.window_mode or "contains").strip().lower()
        self.window_mode = mode if mode in WINDOW_MODES else "contains"

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
            if self._window_allows():
                self._window_blocked = False
                send_keys(kp)
                self.click_count += 1
            else:
                # 前台窗口不匹配：按节拍继续循环，但一个键都不发、不计数
                self._window_blocked = True
            # 按固定节拍推进，避免每次发送的耗时累积成漂移
            next_t += self.interval_ms / 1000.0
            delay = next_t - time.monotonic()
            if delay <= 0:
                next_t = time.monotonic()
                delay = 0.0
            self._stop.wait(delay)

    def to_dict(self):
        return {"type": "click", "keys": self.keys, "interval": self.interval_ms,
                "window_match": self.window_match, "window_mode": self.window_mode}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("keys", "a"), int(d.get("interval", 100)),
                   window_match=d.get("window_match", ""),
                   window_mode=d.get("window_mode", "contains"))


@dataclass
class TimerTask(_BaseTask):
    """定时序列任务：倒计时结束后依次发送 seq 中的按键，然后重新计时。"""

    seq: list = field(default_factory=lambda: ["a"])
    minutes: int = 0
    seconds: int = 10
    gap_ms: int = 200
    remaining: int = 0
    fire_count: int = 0
    # ── v1.2.0 新增（追加在末尾，保持原有位置参数顺序不变）──
    window_match: str = ""
    window_mode: str = "contains"

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
        self.window_match = str(self.window_match or "").strip()
        mode = str(self.window_mode or "contains").strip().lower()
        self.window_mode = mode if mode in WINDOW_MODES else "contains"

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
                    # 倒计时照常走；到点若前台窗口不匹配则不触发、不计次数
                    allowed = self._window_allows()
                    self._window_blocked = not allowed
                    if allowed:
                        self._fire()
                    if not self._stop.is_set():
                        if allowed:
                            self.fire_count += 1
                        self.remaining = self.total_sec
                        next_tick = time.monotonic() + 1.0
            else:
                self._stop.wait(min(0.05, next_tick - now))

    def to_dict(self):
        return {"type": "timer", "seq": list(self.seq),
                "minutes": self.minutes, "seconds": self.seconds, "gap": self.gap_ms,
                "window_match": self.window_match, "window_mode": self.window_mode}

    @classmethod
    def from_dict(cls, d):
        return cls(list(d.get("seq", ["a"])), int(d.get("minutes", 0)),
                   int(d.get("seconds", 10)), int(d.get("gap", 200)),
                   window_match=d.get("window_match", ""),
                   window_mode=d.get("window_mode", "contains"))


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
#  小部件
# ══════════════════════════════════════════════════════════
def _label(text: str = "", role: str = "", fixed_w: int | None = None) -> QLabel:
    """建一个标签并挂上 role 动态属性（颜色等一律交给样式表）。"""
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
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
    样式表由 QApplication 统一提供，这里不再单独设置。
    """

    def __init__(self, parent=None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("按键录入")
        self.setModal(True)
        self.setMinimumWidth(400)
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
            "hint"))
        self.value_lb = _label(self._value or "（等待按键…）", "capture")
        self.value_lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value_lb.setMinimumHeight(52)
        outer.addWidget(self.value_lb)
        self.hint_lb = _label("", "hint")
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
    """单条任务行

    行的外观状态挂在动态属性 ``state`` 上（stopped / running / paused / blocked），
    由样式表按属性选择器上色；刷新时值没变就不重复 setText/setProperty，
    避免 250ms 轮询白白触发重绘。
    """
    playReq = pyqtSignal(object)
    stopReq = pyqtSignal(object)
    delReq  = pyqtSignal(object)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        self.setObjectName("TaskRow")
        self.setFixedHeight(62)
        self._state = ""
        self._build()
        self.refresh()

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(8)

        if isinstance(self.task, ClickTask):
            tag, tag_role = "连点", "tagClick"
        else:
            tag, tag_role = "定时", "tagTimer"
        lay.addWidget(_label(tag, tag_role, 40))

        if isinstance(self.task, ClickTask):
            key_text = self.task.keys
            info_text = f"间隔 {self.task.interval_ms} ms"
        else:
            key_text = "   ·   ".join(self.task.seq) or "(空)"
            m, s = divmod(self.task.total_sec, 60)
            info_text = f"{m:02d}:{s:02d}  间隔 {self.task.gap_ms} ms"

        left = QVBoxLayout()
        left.setSpacing(0)
        self.key_lb = _label(key_text, "key")
        self.info_lb = _label(info_text, "subtle")
        self.window_lb = _label("", "window")
        self.window_lb.setVisible(False)
        left.addWidget(self.key_lb)
        left.addWidget(self.info_lb)
        left.addWidget(self.window_lb)
        lay.addLayout(left, 1)

        self.status_lb = _label("○ 停止", "status", 62)
        lay.addWidget(self.status_lb)
        self.prog_lb = _label("", "progress", 54)
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

    # ── 只改"变了"的属性，省掉无意义的重绘 ──
    @staticmethod
    def _set_text(lb, text: str):
        if lb.text() != text:
            lb.setText(text)

    @staticmethod
    def _set_role(lb, role: str):
        if lb.property("role") != role:
            lb.setProperty("role", role)
            repolish(lb)

    def _set_state(self, state: str):
        if self._state != state:
            self._state = state
            self.setProperty("state", state)
            repolish(self)

    def refresh(self):
        t = self.task
        running, paused = t.running, t.paused
        blocked = bool(running and not paused and t.window_blocked)
        if blocked:
            state, status = "blocked", "● 运行"
        elif running and not paused:
            state, status = "running", "● 运行"
        elif paused:
            state, status = "paused", "● 暂停"
        else:
            state, status = "stopped", "○ 停止"
        self._set_state(state)
        self._set_text(self.status_lb, status)

        if isinstance(t, ClickTask):
            self._set_text(self.prog_lb, str(t.click_count))
        else:
            m, s = divmod(max(t.remaining, 0), 60)
            self._set_text(self.prog_lb, f"{m:02d}:{s:02d}")

        pattern = (t.window_match or "").strip()
        if not pattern:
            if not self.window_lb.isHidden():
                self.window_lb.setVisible(False)
        else:
            if blocked:
                self._set_text(self.window_lb, "窗口不匹配（当前前台窗口未命中）")
                self._set_role(self.window_lb, "warn")
            else:
                label = WINDOW_MODE_LABELS.get(t.window_mode, "包含")
                self._set_text(self.window_lb, f"窗口{label}：{pattern}")
                self._set_role(self.window_lb, "window")
            if self.window_lb.isHidden():
                self.window_lb.setVisible(True)

        # isHidden() 反映"是否被显式隐藏"，父窗口尚未显示时依然准确
        show_stop = bool(running or paused)
        if show_stop == self.btn_stop.isHidden():
            self.btn_stop.setVisible(show_stop)

    def is_running(self):
        return self.task.running or self.task.paused


class SequenceEditor(QDialog):
    """录入 / 编辑按键序列"""

    def __init__(self, parent=None, existing=None):
        super().__init__(parent)
        self.setWindowTitle("按键序列")
        self.setModal(True)
        self.setMinimumWidth(460)
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
        outer.addWidget(_label("按键序列（录制时按 Esc 结束）：", "hint"))

        self.seq_lb = _label("", "seqbox")
        self.seq_lb.setMinimumHeight(46)
        self.seq_lb.setWordWrap(True)
        outer.addWidget(self.seq_lb)

        self.rec_btn = QPushButton("● 开始录入")
        self.rec_btn.setObjectName("Warn")
        self.rec_btn.clicked.connect(self._toggle_recording)
        outer.addWidget(self.rec_btn)

        outer.addWidget(_label("或手动输入（逗号分隔，支持组合键 如  ctrl+shift+f5）：",
                               "hint"))
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
        repolish(self.rec_btn)
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
        repolish(self.rec_btn)
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
class WindowCombo(QComboBox):
    """可编辑下拉框：点开时实时列出当前所有可见窗口标题，也能直接手输。

    特意保留了 QLineEdit 的常用接口（text / setText / textChanged），
    这样它可以直接顶替原来的输入框，外部代码不用改。
    """

    textChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMaxVisibleItems(18)
        self.setMinimumContentsLength(18)
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        try:
            self.lineEdit().setPlaceholderText("留空 = 不限制")
            self.lineEdit().setClearButtonEnabled(True)
        except Exception:
            pass
        self.editTextChanged.connect(self.textChanged)

    # ── QLineEdit 兼容接口 ──
    def text(self) -> str:
        return self.currentText()

    def setText(self, value) -> None:
        try:
            self.setEditText("" if value is None else str(value))
        except Exception:
            pass

    def refresh_windows(self) -> int:
        """重新枚举窗口标题填入下拉列表，保留用户已输入的内容。"""
        current = self.currentText()
        titles = list_window_titles()
        blocked = self.blockSignals(True)
        try:
            self.clear()
            self.addItems(titles)
            self.setEditText(current)
        finally:
            self.blockSignals(blocked)
        return len(titles)

    def showPopup(self):
        # 每次展开都刷新一遍，避免列出早已关闭的窗口
        self.refresh_windows()
        super().showPopup()

    def wheelEvent(self, event):
        # 放在滚动区域内时，滚轮不应该顺手改掉窗口关键词
        event.ignore()


class PagePanel(QWidget):
    dataChanged = pyqtSignal()

    def __init__(self, page: Page, parent=None):
        super().__init__(parent)
        self.page = page
        self.rows: list[TaskRow] = []
        self._timer_seq: list = []
        self._stat_text = ""
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
        self.mode_combo.setToolTip(
            "任务类型：\n"
            "· 连点 —— 按固定间隔反复发送同一个按键\n"
            "· 定时序列 —— 倒计时结束后依次发送序列里的按键")
        self.mode_combo.setCursor(Qt.CursorShape.PointingHandCursor)
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

        # 定时序列参数（整块用强调色底托起来，浅色/深色下都一眼可辨）
        self.timer_grp = QWidget()
        self.timer_grp.setObjectName("TimerGroup")
        tv = QVBoxLayout(self.timer_grp)
        tv.setContentsMargins(12, 10, 12, 12)
        tv.setSpacing(8)
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
        self.seq_btn.setObjectName("SeqBtn")
        self.seq_btn.setProperty("filled", "false")
        self.seq_btn.clicked.connect(self._edit_seq)
        r2.addWidget(self.seq_btn, 1)
        tv.addLayout(r2)

        self.timer_grp.hide()
        al.addWidget(self.timer_grp)

        # 前台窗口匹配（连点 / 定时都适用）：可编辑下拉，点开即列出当前窗口
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        r3.addWidget(_label("仅在前台窗口匹配时生效"))
        self.window_edit = WindowCombo()
        self.window_edit.setPlaceholderText("留空 = 不限制；点开下拉选窗口，也可直接输入关键词")
        self.window_edit.setToolTip(
            "点开下拉框会实时列出当前所有可见窗口的标题，选中即可；\n"
            "也可以直接输入关键词，配合右侧「包含 / 精确 / 正则」使用")
        r3.addWidget(self.window_edit, 1)
        self.window_mode_combo = QComboBox()
        for m in WINDOW_MODES:
            self.window_mode_combo.addItem(WINDOW_MODE_LABELS[m], m)
        self.window_mode_combo.setFixedWidth(92)
        self.window_mode_combo.setToolTip("窗口标题的匹配方式：包含 / 精确 / 正则")
        self.window_mode_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        r3.addWidget(self.window_mode_combo)
        self.window_grab_btn = QPushButton("抓取当前窗口")
        self.window_grab_btn.setObjectName("Ghost")
        self.window_grab_btn.setToolTip("把当前前台窗口的标题填入左侧输入框")
        self.window_grab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.window_grab_btn.clicked.connect(self._grab_window)
        r3.addWidget(self.window_grab_btn)
        al.addLayout(r3)

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
        head.addWidget(_label("任务列表", "cardTitle"))
        head.addStretch(1)
        self.stat_lb = _label("", "subtle")
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
        self.container.setObjectName("TaskContainer")
        self.task_layout = QVBoxLayout(self.container)
        self.task_layout.setContentsMargins(0, 0, 4, 0)
        self.task_layout.setSpacing(6)
        self.empty_lb = _label("暂无任务，请在上方添加", "muted")
        self.empty_lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.task_layout.addWidget(self.empty_lb)
        self.task_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        ll.addWidget(self.scroll, 1)
        root.addWidget(list_card, 1)

    def _mode_changed(self, i):
        self.click_grp.setVisible(i == 0)
        self.timer_grp.setVisible(i == 1)

    def _grab_window(self):
        """把当前前台窗口标题写入输入框，并在状态栏提示窗口所属程序。"""
        title = foreground_window_title()
        if not title:
            QMessageBox.information(self, "提示", "未能读取当前前台窗口标题。")
            return
        self.window_edit.refresh_windows()   # 顺手把最新窗口列表刷进下拉
        self.window_edit.setText(title)
        if not title in [self.window_edit.itemText(i)
                         for i in range(self.window_edit.count())]:
            # 下拉里没有（例如抓的是本程序自己的窗口）就补一条，避免看起来"没抓到"
            self.window_edit.insertItem(0, title)
        proc = foreground_process_name()
        msg = f"已抓取窗口标题：{title}" + (f"（{proc}）" if proc else "")
        parent = self.window()
        try:
            if hasattr(parent, "statusBar"):
                parent.statusBar().showMessage(msg, 4000)
        except Exception:
            pass

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
            filled = "true"
        else:
            self.seq_btn.setText("点击设置序列")
            filled = "false"
        if self.seq_btn.property("filled") != filled:
            self.seq_btn.setProperty("filled", filled)
            repolish(self.seq_btn)

    def _window_settings(self) -> tuple[str, str]:
        """读取界面上当前的窗口匹配设置。"""
        pattern = self.window_edit.text().strip()
        idx = self.window_mode_combo.currentIndex()
        mode = WINDOW_MODES[idx] if 0 <= idx < len(WINDOW_MODES) else "contains"
        return pattern, mode

    def _add_task(self):
        pattern, mode = self._window_settings()
        if pattern and mode == "regex":
            try:
                re.compile(pattern)
            except re.error:
                QMessageBox.warning(self, "正则表达式无效",
                                    "窗口标题的正则表达式无法编译，请检查后重试。")
                return
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
            task = ClickTask(key_text, ms, window_match=pattern, window_mode=mode)
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
            task = TimerTask(list(self._timer_seq), m, s, gap,
                             window_match=pattern, window_mode=mode)
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
        text = f"{n} 个任务  ·  {run} 运行中"
        if text != self._stat_text:
            self._stat_text = text
            self.stat_lb.setText(text)

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
        # 程序图标（logo.png 缺失时自动降级为无图标，不影响运行）
        _icon = app_icon()
        if not _icon.isNull():
            self.setWindowIcon(_icon)
        # 点窗口空白处时让输入框失焦（否则聚焦边框会一直挂着）
        install_focus_clearer()
        self.pages: list[Page] = []
        self.panels: list[PagePanel] = []
        self.theme_name = DEFAULT_THEME
        self._cfg_theme: str | None = None
        self._build()
        self._load_config()
        # 配置里没有主题时跟随系统（首次运行）
        self.set_theme(self._cfg_theme or system_theme(), apply=False)
        if not self.pages:
            self.pages = [Page("配置1")]
        self._rebuild_stack()
        ensure_config_dir(CONFIG_PATH)
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
        hl.setContentsMargins(20, 12, 20, 10)
        hl.setSpacing(10)
        hl.addWidget(_label(APP_NAME, "title"))
        hl.addWidget(_label("连点 · 定时序列 · 多配置页 · 窗口锁定", "subtitle"))
        hl.addStretch(1)
        self.btn_options = QPushButton("选项")
        self.btn_options.setObjectName("Ghost")
        self._build_options_menu()
        hl.addWidget(self.btn_options)
        self.btn_theme = QPushButton("")
        self.btn_theme.setObjectName("Ghost")
        self.btn_theme.setToolTip("在浅色 / 深色主题之间切换（会自动记住）")
        self.btn_theme.clicked.connect(self._toggle_theme)
        hl.addWidget(self.btn_theme)
        root.addWidget(head)

        sep = QFrame()
        sep.setObjectName("HeaderSep")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        # 操作栏
        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(20, 10, 20, 10)
        bl.setSpacing(8)
        bl.addWidget(_label("当前页", "muted"))

        self.page_combo = QComboBox()
        self.page_combo.setFixedWidth(140)
        self.page_combo.setToolTip("切换配置页：每一页都有自己独立的任务列表")
        self.page_combo.setCursor(Qt.CursorShape.PointingHandCursor)
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
        b_del = QPushButton("✕ 删除页")
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

        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage(
            "就绪   ·   热键 Ctrl+` 开始/停止当前页，F6 全部停止   ·   配置自动保存")

    def _build_options_menu(self):
        menu = QMenu(self)
        act_dir = menu.addAction("打开配置目录")
        act_dir.triggered.connect(self._open_config_dir)
        menu.addSeparator()
        act_about = menu.addAction("关于")
        act_about.triggered.connect(self._show_about)
        self.btn_options.setMenu(menu)

    def _center(self):
        scr = QApplication.primaryScreen()
        if scr:
            geo = self.frameGeometry()
            geo.moveCenter(scr.availableGeometry().center())
            self.move(geo.topLeft())

    # ── 主题 ──
    def _sync_theme_button(self):
        other = "浅色" if self.theme_name == "dark" else "深色"
        text = f"切换到{other}"
        if self.btn_theme.text() != text:
            self.btn_theme.setText(text)

    def set_theme(self, name: str, apply: bool = True) -> str:
        """切换主题并同步按钮文字；apply=True 时立刻整窗重新应用样式表。"""
        self.theme_name = set_current_theme(name)
        self._sync_theme_button()
        if apply:
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(make_qss(self.theme_name))
        return self.theme_name

    def _toggle_theme(self):
        self.set_theme("light" if self.theme_name == "dark" else "dark")
        self._save()

    # ── 选项菜单 ──
    def _open_config_dir(self):
        try:
            directory = ensure_config_dir(CONFIG_PATH)
            os.startfile(directory)      # 失败/非 Windows 一律静默
        except Exception:
            pass

    def _show_about(self):
        text = (f"{APP_NAME}  v{__version__}\n\n"
                f"配置文件：\n{CONFIG_PATH}\n\n"
                "全局热键：\n"
                "  Ctrl+`   开始 / 停止当前配置页\n"
                "  F6       全部停止（紧急停止）\n\n"
                "纯本地程序，不联网：不采集、不上传任何数据。")
        QMessageBox.information(self, "关于", text)

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
        # 只刷新当前可见面板，避免后台页白白重绘；
        # 行内刷新只写"变了"的值，值没变不会触发重绘。
        panel = self._current_panel()
        if panel is not None:
            panel._refresh_rows()
            panel._refresh_stats()

    def _save(self):
        try:
            data = {
                "version": CONFIG_VERSION,
                "theme": self.theme_name,
                "pages": [p.to_dict() for p in self.pages],
            }
            ensure_config_dir(CONFIG_PATH)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)  # 原子替换，避免写一半损坏配置
        except Exception:
            pass

    def _load_config(self):
        """读取配置：兼容 v1.1.0 的旧格式（顶层直接是页面数组）。"""
        self._cfg_theme = None
        try:
            if not os.path.exists(CONFIG_PATH):
                return
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if isinstance(data, dict):
            theme = data.get("theme")
            if theme in THEMES:
                self._cfg_theme = theme
            pages = data.get("pages", [])
        elif isinstance(data, list):
            pages = data                      # v1.1.0 旧格式
        else:
            pages = []
        if isinstance(pages, list):
            self.pages = [Page.from_dict(d) for d in pages if isinstance(d, dict)]

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


def _set_timer_precision(on: bool) -> None:
    """开启/关闭 1ms 计时精度；非 Windows 或失败一律静默忽略。"""
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        if on:
            winmm.timeBeginPeriod(1)
        else:
            winmm.timeEndPeriod(1)
    except Exception:
        pass


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    _set_timer_precision(True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    _icon = app_icon()
    if not _icon.isNull():
        app.setWindowIcon(_icon)
    install_focus_clearer(app)
    win = MainWindow()
    app.setStyleSheet(make_qss())   # 整个应用只在这里应用一次样式表
    win.show()
    try:
        code = app.exec()
    finally:
        _set_timer_precision(False)
    sys.exit(code)


if __name__ == "__main__":
    main()
