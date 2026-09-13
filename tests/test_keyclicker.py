"""keyclicker 自动化测试。

运行方式（在项目根目录下）：
    python -m unittest discover -s tests -v

说明：UI 相关测试使用 Qt 的 offscreen 平台插件，不会真的弹出窗口；
     所有测试都不会真正向系统发送按键（使用不存在的键名做空转）。
     涉及配置路径的用例全部使用临时目录并临时改写环境变量，
     绝不会读写真实的用户配置目录。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 无头运行

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import keyclicker as kc  # noqa: E402
from PyQt6.QtCore import QEvent, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = None
FAKE_KEY = "notarealkey"  # keyboard 无法识别 → 不会真的按键


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication([])


class _FakeBox:
    """替代 QMessageBox，避免测试时弹出模态框。"""

    calls: list = []

    @classmethod
    def _record(cls, kind, *args, **kwargs):
        cls.calls.append((kind, args))
        return None

    @classmethod
    def warning(cls, *args, **kwargs):
        return cls._record("warning", *args, **kwargs)

    @classmethod
    def information(cls, *args, **kwargs):
        return cls._record("information", *args, **kwargs)

    @classmethod
    def question(cls, *args, **kwargs):
        return cls._record("question", *args, **kwargs)


class _NoDialog:
    """临时替换模块内的 QMessageBox。"""

    def __enter__(self):
        self._orig = kc.QMessageBox
        _FakeBox.calls.clear()
        kc.QMessageBox = _FakeBox
        return _FakeBox

    def __exit__(self, *exc):
        kc.QMessageBox = self._orig
        return False


def _rgb_key(color) -> int:
    """颜色 → 0xRRGGBB 整数，便于和 QImage 像素直接比较。"""
    if isinstance(color, str):
        return int(color.lstrip("#"), 16)
    return int(color.rgb()) & 0xFFFFFF


class _TempConfig:
    """把配置写到临时文件，避免污染真实配置文件。"""

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig = kc.CONFIG_PATH
        kc.CONFIG_PATH = self.path
        return self.path

    def __exit__(self, *exc):
        kc.CONFIG_PATH = self._orig
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


class _AppDirStub:
    """把系统临时目录当作"程序目录"来用。

    受限环境下无法在新建的子目录里写文件，所以这里直接在已存在的临时目录
    中放置具名标记文件来模拟便携模式，退出时清理干净：
    全程只使用临时目录，绝不写入真实的用户配置目录。
    """

    MARKERS = ("clicker_config.json", "portable.flag")

    def __init__(self, *markers: str):
        self.dir = tempfile.gettempdir()
        self.want = list(markers)

    def __enter__(self) -> str:
        self._cleanup()
        for name in self.want:
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write("{}")
        return self.dir

    def __exit__(self, *exc):
        self._cleanup()
        return False

    def _cleanup(self) -> None:
        for name in self.MARKERS:
            try:
                os.remove(os.path.join(self.dir, name))
            except OSError:
                pass


def _fake_appdata() -> str:
    """假的 %APPDATA% 路径（只用于拼接期望值，不产生任何写入）。"""
    return os.path.join(tempfile.gettempdir(), "kc_fake_appdata")


class _EnvPatch:
    """临时改写环境变量（值为 None 表示删除该变量）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.old: dict = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class _StubForeground:
    """把前台窗口标题打桩成固定值。"""

    def __init__(self, title):
        self.title = title

    def __enter__(self):
        self._orig = kc.foreground_window_title
        kc.foreground_window_title = lambda: self.title
        return self

    def __exit__(self, *exc):
        kc.foreground_window_title = self._orig
        return False


# ══════════════════════════════════════════════════════════
#  1. 按键名解析与校验
# ══════════════════════════════════════════════════════════
class KeyNameTests(unittest.TestCase):
    def test_alias_normalization(self):
        cases = {
            "WIN": "windows", "Super": "windows", "cmd": "windows",
            "command": "windows", "control": "ctrl", "ctl": "ctrl",
            "printscreen": "print screen", "prtscn": "print screen",
            "pgup": "page up", "pgdn": "page down", "ins": "insert",
            "scrolllock": "scroll lock", "winleft": "left windows",
            "  F9  ": "f9", "Ctrl": "ctrl",
        }
        for raw, want in cases.items():
            self.assertEqual(kc.normalize_key(raw), want, raw)

    def test_parse_keys(self):
        self.assertEqual(kc.parse_keys("ctrl+f9"), ["ctrl", "f9"])
        self.assertEqual(kc.parse_keys("Ctrl + Shift + F5"), ["ctrl", "shift", "f5"])
        self.assertEqual(kc.parse_keys("win+e"), ["windows", "e"])
        self.assertEqual(kc.parse_keys(""), [])
        self.assertEqual(kc.parse_keys("   "), [])
        self.assertEqual(kc.parse_keys("a++b"), ["a", "b"])

    def test_validate_ok(self):
        keys, err = kc.validate_keys("ctrl+f9")
        self.assertIsNone(err)
        self.assertEqual(keys, ["ctrl", "f9"])
        # 旧配置里可能出现的写法也要能通过
        for raw in ("a", "win", "escape", "page up", "space", "enter", "caps lock"):
            _, err = kc.validate_keys(raw)
            self.assertIsNone(err, raw)

    def test_validate_errors(self):
        _, err = kc.validate_keys("")
        self.assertIn("不能为空", err)
        _, err = kc.validate_keys("foobar")
        self.assertIn("无法识别", err)
        _, err = kc.validate_keys("ctrl+foobar")
        self.assertIn("foobar", err)
        _, err = kc.validate_keys("+".join(f"f{i}" for i in range(1, 12)))
        self.assertIn("最多", err)

    def test_is_valid_key(self):
        self.assertTrue(kc.is_valid_key("windows"))
        self.assertTrue(kc.is_valid_key("page up"))
        self.assertTrue(kc.is_valid_key("f9"))
        self.assertFalse(kc.is_valid_key(FAKE_KEY))

    def test_format_keys(self):
        self.assertEqual(kc.format_keys(["left ctrl", "f5"]), "ctrl+f5")
        self.assertEqual(kc.format_keys(["shift", "left ctrl"]), "ctrl+shift")
        self.assertEqual(kc.format_keys([]), "")
        # 归一化后的文本应能被重新解析
        for raw in ("ctrl+f9", "shift+a", "windows+e", "page up"):
            keys = kc.parse_keys(raw)
            again = kc.parse_keys(kc.format_keys(keys))
            self.assertEqual(again, keys, raw)

    def test_send_keys_invalid_returns_false(self):
        self.assertFalse(kc.send_keys([FAKE_KEY]))   # 不发任何真实按键
        self.assertFalse(kc.send_keys([]))
        self.assertFalse(kc.send_keys(["", None]))


# ══════════════════════════════════════════════════════════
#  2. 任务模型
# ══════════════════════════════════════════════════════════
class TaskTests(unittest.TestCase):
    def test_click_task_normalizes(self):
        t = kc.ClickTask("Ctrl+F9", 100)
        self.assertEqual(t.keys, "ctrl+f9")
        self.assertEqual(t.interval_ms, 100)

    def test_click_task_defaults_and_clamp(self):
        self.assertEqual(kc.ClickTask("").keys, "a")
        self.assertEqual(kc.ClickTask("   ").keys, "a")
        self.assertEqual(kc.ClickTask("a", 0).interval_ms, kc.MIN_INTERVAL_MS)
        self.assertEqual(kc.ClickTask("a", -50).interval_ms, kc.MIN_INTERVAL_MS)

    def test_click_task_runs_and_stops(self):
        t = kc.ClickTask(FAKE_KEY, 20)
        t.start()
        time.sleep(0.15)
        self.assertTrue(t.running)
        self.assertGreaterEqual(t.click_count, 2)
        t.stop()
        self.assertFalse(t.running)
        count = t.click_count
        time.sleep(0.1)
        self.assertEqual(t.click_count, count)   # 停止后不再累加

    def test_click_task_restart_is_clean(self):
        t = kc.ClickTask(FAKE_KEY, 20)
        t.start()
        t.start()          # 重复 start 不应产生第二个线程
        time.sleep(0.1)
        t.stop()
        t.start()
        time.sleep(0.1)
        t.stop()
        self.assertFalse(t.running)

    def test_click_task_pause(self):
        t = kc.ClickTask(FAKE_KEY, 20)
        t.start()
        time.sleep(0.1)
        t.toggle_pause()
        self.assertTrue(t.paused)
        paused_at = t.click_count
        time.sleep(0.15)
        self.assertLessEqual(t.click_count - paused_at, 1)  # 暂停后基本不增长
        t.toggle_pause()
        time.sleep(0.15)
        self.assertGreater(t.click_count, paused_at)
        t.stop()

    def test_timer_task_normalizes(self):
        t = kc.TimerTask(["Ctrl+A", "", "  ", "b"], 0, 5, 5)
        self.assertEqual(t.seq, ["ctrl+a", "b"])      # 空项被丢弃
        self.assertEqual(t.gap_ms, kc.MIN_INTERVAL_MS)
        self.assertEqual(t.total_sec, 5)
        self.assertEqual(t.remaining, 5)

    def test_timer_task_empty_seq_fallback(self):
        self.assertEqual(kc.TimerTask([], 0, 3).seq, ["a"])

    def test_timer_task_time_math(self):
        t = kc.TimerTask(["a"], 1, 30)
        self.assertEqual(t.total_sec, 90)
        self.assertEqual(t.remaining, 90)
        t = kc.TimerTask(["a"], -1, -5)
        self.assertEqual(t.total_sec, 1)              # 至少 1 秒
        self.assertEqual(t.remaining, 1)

    def test_timer_task_fires_and_loops(self):
        t = kc.TimerTask([FAKE_KEY], 0, 1, kc.MIN_INTERVAL_MS)
        t.start()
        deadline = time.time() + 3.0
        while t.fire_count < 1 and time.time() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(t.fire_count, 1)
        self.assertEqual(t.remaining, 1)              # 触发后重新开始倒计时
        t.stop()
        self.assertFalse(t.running)


# ══════════════════════════════════════════════════════════
#  3. 配置读写
# ══════════════════════════════════════════════════════════
class ConfigTests(unittest.TestCase):
    def test_page_roundtrip(self):
        page = kc.Page("配置A", [
            kc.ClickTask("ctrl+f9", 250),
            kc.TimerTask(["a", "shift+b"], 0, 30, 300),
        ])
        data = json.loads(json.dumps(page.to_dict(), ensure_ascii=False))
        back = kc.Page.from_dict(data)
        self.assertEqual(back.name, "配置A")
        self.assertEqual(len(back.tasks), 2)
        self.assertIsInstance(back.tasks[0], kc.ClickTask)
        self.assertEqual(back.tasks[0].keys, "ctrl+f9")
        self.assertEqual(back.tasks[0].interval_ms, 250)
        self.assertIsInstance(back.tasks[1], kc.TimerTask)
        self.assertEqual(back.tasks[1].seq, ["a", "shift+b"])
        self.assertEqual(back.tasks[1].total_sec, 30)
        self.assertEqual(back.tasks[1].gap_ms, 300)

    def test_legacy_and_broken_config(self):
        legacy = {
            "name": "配置1",
            "tasks": [
                {"type": "click", "keys": "win+x", "interval": 50},
                {"type": "timer", "seq": ["ctrl+c"], "minutes": 0,
                 "seconds": 3, "gap": 200},
                {"type": "unknown", "keys": "a"},
                "垃圾数据",
            ],
        }
        page = kc.Page.from_dict(legacy)
        self.assertEqual(len(page.tasks), 2)          # 非法任务被跳过
        self.assertEqual(page.tasks[0].keys, "windows+x")
        self.assertEqual(page.tasks[0].interval_ms, 50)
        self.assertEqual(page.tasks[1].seq, ["ctrl+c"])


# ══════════════════════════════════════════════════════════
#  4. 按键录入器（核心 bug 回归测试）
# ══════════════════════════════════════════════════════════
class _FakeEvent:
    def __init__(self, name, event_type="down"):
        self.name = name
        self.event_type = event_type
        self.scan_code = 0
        self.time = 0.0
        self.is_keypad = False


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.rec = kc.KeyRecorder()
        self.got: list = []
        self.cancelled: list = []
        self.rec.captured.connect(self.got.append)
        self.rec.cancelled.connect(lambda: self.cancelled.append(True))

    def tearDown(self):
        self.rec.stop()

    def _down(self, name):
        self.rec._on_event(_FakeEvent(name, "down"))

    def _up(self, name):
        self.rec._on_event(_FakeEvent(name, "up"))

    def test_single_key(self):
        self._down("a")
        self._up("a")
        self.assertEqual(self.got, ["a"])

    def test_modifier_alone_is_ignored(self):
        self._down("left ctrl")
        self._up("left ctrl")
        self._down("left shift")
        self._up("left shift")
        self.assertEqual(self.got, [])
        self.assertEqual(self.cancelled, [])

    def test_combo_keeps_modifiers(self):
        """回归：Windows 钩子给出 'left ctrl'，旧代码判断 'ctrl' 永远不成立。"""
        self._down("left ctrl")
        self._down("f5")
        self._up("f5")
        self._up("left ctrl")
        self.assertEqual(self.got, ["ctrl+f5"])

    def test_multi_modifier_combo(self):
        self._down("left ctrl")
        self._down("left shift")
        self._down("f9")
        self.assertEqual(self.got, [kc.format_keys(["left ctrl", "left shift", "f9"])])
        self.assertIn("ctrl", self.got[0])
        self.assertIn("shift", self.got[0])
        self.assertIn("f9", self.got[0])

    def test_windows_key_is_modifier(self):
        self._down("left windows")
        self._down("e")
        self.assertEqual(self.got, ["windows+e"])

    def test_auto_repeat_ignored(self):
        for _ in range(12):          # 长按产生的重复 down 事件
            self._down("a")
        self.assertEqual(self.got, ["a"])

    def test_esc_cancels(self):
        self._down("esc")
        self.assertEqual(self.cancelled, [True])
        self.assertEqual(self.got, [])

    def test_keys_after_release_recorded_again(self):
        self._down("a")
        self._up("a")
        self._down("a")
        self.assertEqual(self.got, ["a", "a"])

    def test_stop_is_idempotent(self):
        self.rec.stop()
        self.rec.stop()
        self.assertFalse(self.rec.active)


# ══════════════════════════════════════════════════════════
#  5. 界面（offscreen）
# ══════════════════════════════════════════════════════════
class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _TempConfig()
        self.cfg.__enter__()

    def tearDown(self):
        self.cfg.__exit__(None, None, None)

    def test_main_window_builds(self):
        win = kc.MainWindow()
        try:
            self.assertTrue(win.pages)
            self.assertEqual(len(win.panels), len(win.pages))
            panel = win.panels[0]
            self.assertTrue(hasattr(panel, "click_rec_btn"))   # 连点录入按钮
            self.assertEqual(panel.click_key.text(), "a")
            self.assertEqual(panel.seq_btn.text(), "点击设置序列")
        finally:
            win.close()

    def test_add_click_task_validates_keys(self):
        win = kc.MainWindow()
        try:
            panel = win.panels[0]
            with _NoDialog() as box:
                panel.click_key.setText("foobar")
                panel._add_task()
                self.assertTrue(box.calls)                      # 有报错提示
            self.assertEqual(len(panel.page.tasks), 0)
            panel.click_key.setText("Ctrl+F9")
            panel.click_interval.setText("0")                   # 低于下限
            panel._add_task()
            self.assertEqual(len(panel.page.tasks), 1)
            task = panel.page.tasks[0]
            self.assertIsInstance(task, kc.ClickTask)
            self.assertEqual(task.keys, "ctrl+f9")
            self.assertEqual(task.interval_ms, kc.MIN_INTERVAL_MS)
        finally:
            win.close()

    def test_add_timer_task_requires_sequence(self):
        win = kc.MainWindow()
        try:
            panel = win.panels[0]
            panel.mode_combo.setCurrentIndex(1)
            with _NoDialog() as box:
                panel._add_task()
                self.assertTrue(box.calls)
            self.assertEqual(len(panel.page.tasks), 0)
            panel._timer_seq = ["a", "ctrl+f9"]
            panel._add_task()
            self.assertEqual(len(panel.page.tasks), 1)
            self.assertEqual(panel.page.tasks[0].seq, ["a", "ctrl+f9"])
        finally:
            win.close()

    def test_sequence_editor_manual_input(self):
        dlg = kc.SequenceEditor(None, ["ctrl+f9"])
        try:
            self.assertEqual(dlg.seq(), ["ctrl+f9"])
            dlg.manual_edit.setText("a, ctrl+shift+f5 , win+e")
            dlg._append_manual()
            self.assertEqual(dlg.seq(), ["ctrl+f9", "a", "ctrl+shift+f5", "windows+e"])
            with _NoDialog() as box:
                dlg.manual_edit.setText("zzz")
                dlg._append_manual()
                self.assertTrue(box.calls)                      # 非法键名被拒绝
            self.assertEqual(dlg.seq(), ["ctrl+f9", "a", "ctrl+shift+f5", "windows+e"])
        finally:
            dlg._stop_recording()
            dlg.deleteLater()

    def test_key_capture_dialog_value(self):
        dlg = kc.KeyCaptureDialog(None, "ctrl+f9")
        try:
            self.assertEqual(dlg.value(), "ctrl+f9")
            dlg._on_captured("shift+a")
            self.assertEqual(dlg.value(), "shift+a")
        finally:
            dlg.reject()

    def test_panic_hotkey_stops_all_pages(self):
        win = kc.MainWindow()
        try:
            if len(win.pages) < 2:
                win._add_page()
            tasks = []
            for panel in win.panels:
                t = kc.ClickTask(FAKE_KEY, 20)
                panel.page.tasks.append(t)
                t.start()
                tasks.append(t)
            time.sleep(0.1)
            self.assertTrue(all(t.running for t in tasks))
            win._hotkey_panic()
            self.assertFalse(any(t.running for t in tasks))
        finally:
            win.close()

    def test_hotkeys_suspended_while_recording(self):
        """录入期间全局热键必须失效，否则录 F6 会先触发"全部停止"。"""
        win = kc.MainWindow()
        rec = kc.KeyRecorder()
        started = rec.start()
        try:
            if not started:
                self.skipTest("本机键盘钩子不可用")
            self.assertTrue(kc.is_recording())
            t = kc.ClickTask(FAKE_KEY, 20)
            win.panels[0].page.tasks.append(t)
            t.start()
            time.sleep(0.1)
            self.assertTrue(t.running)
            win._hotkey_panic()
            self.assertTrue(t.running, "录入期间 F6 不应停止任务")
            win._hotkey_toggle()
            self.assertTrue(t.running, "录入期间 Ctrl+` 不应切换任务")
            rec.start()                 # 重复 start 不应重复计数
            self.assertTrue(kc.is_recording())
            rec.stop()
            self.assertFalse(kc.is_recording())
            win._hotkey_panic()         # 停止录入后热键恢复
            self.assertFalse(t.running)
        finally:
            rec.stop()
            self.assertFalse(kc.is_recording())
            win.close()

    def test_config_saved_on_close(self):
        win = kc.MainWindow()
        panel = win.panels[0]
        panel.click_key.setText("ctrl+f9")
        panel._add_task()
        win.close()
        with open(kc.CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # v1.2.0 新格式：{"version": 2, "theme": ..., "pages": [...]}
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("version"), kc.CONFIG_VERSION)
        self.assertIn(data.get("theme"), kc.THEME_NAMES)
        self.assertIsInstance(data.get("pages"), list)
        self.assertTrue(data["pages"])
        keys = [t.get("keys") for p in data["pages"] for t in p.get("tasks", [])]
        self.assertIn("ctrl+f9", keys)

    def test_poll_and_page_switch(self):
        win = kc.MainWindow()
        try:
            win._poll()
            win._add_page()
            win._switch_page(1)
            self.assertEqual(win.stack.currentIndex(), 1)
            win._switch_page(99)          # 越界不应崩溃
            self.assertEqual(win.stack.currentIndex(), 1)
        finally:
            win.close()

    def test_rename_and_delete_page_guards(self):
        win = kc.MainWindow()
        try:
            win._add_page()
            n = len(win.pages)
            win._del_page()
            self.assertEqual(len(win.pages), n - 1)
            while len(win.pages) > 1:
                win._del_page()
            with _NoDialog() as box:
                win._del_page()             # 只剩一页时给出提示
                self.assertTrue(box.calls)
            self.assertEqual(len(win.pages), 1)
        finally:
            win.close()


# ══════════════════════════════════════════════════════════
#  6. 窗口标题匹配（纯函数）
# ══════════════════════════════════════════════════════════
class WindowMatchTests(unittest.TestCase):
    TITLE = "无标题 - 记事本"

    def test_empty_pattern_matches_anything(self):
        for mode in kc.WINDOW_MODES:
            self.assertTrue(kc.match_window("", self.TITLE, mode))
            self.assertTrue(kc.match_window("   ", self.TITLE, mode))
            self.assertTrue(kc.match_window(None, "", mode))

    def test_contains(self):
        self.assertTrue(kc.match_window("记事本", self.TITLE, "contains"))
        self.assertFalse(kc.match_window("浏览器", self.TITLE, "contains"))
        self.assertTrue(kc.match_window("标题", self.TITLE, "contains"))

    def test_exact(self):
        self.assertTrue(kc.match_window(self.TITLE, self.TITLE, "exact"))
        self.assertFalse(kc.match_window("记事本", self.TITLE, "exact"))
        self.assertFalse(kc.match_window(self.TITLE + "x", self.TITLE, "exact"))

    def test_regex(self):
        self.assertTrue(kc.match_window(r"^无标题.*记事本$", self.TITLE, "regex"))
        self.assertTrue(kc.match_window(r"记事本|计算器", self.TITLE, "regex"))
        self.assertFalse(kc.match_window(r"^\d+$", self.TITLE, "regex"))

    def test_invalid_regex_returns_false_without_raising(self):
        self.assertFalse(kc.match_window("([", self.TITLE, "regex"))
        self.assertFalse(kc.match_window("a{2,1}", self.TITLE, "regex"))

    def test_case_insensitive(self):
        self.assertTrue(kc.match_window("notepad", "Untitled - Notepad", "contains"))
        self.assertTrue(kc.match_window("NOTEPAD", "Untitled - Notepad", "contains"))
        self.assertTrue(kc.match_window("notepad", "Notepad", "exact"))
        self.assertTrue(kc.match_window("notepad", "Untitled - NotePad", "regex"))

    def test_unknown_mode_falls_back_to_contains(self):
        self.assertTrue(kc.match_window("记事本", self.TITLE, "不存在的模式"))
        self.assertTrue(kc.match_window("记事本", self.TITLE, ""))

    def test_foreground_helpers_never_raise(self):
        self.assertIsInstance(kc.foreground_window_title(), str)
        self.assertIsInstance(kc.foreground_process_name(), str)


# ══════════════════════════════════════════════════════════
#  7. 主题
# ══════════════════════════════════════════════════════════
class ThemeTests(unittest.TestCase):
    def test_themes_differ_and_contain_key_colors(self):
        light = kc.make_qss("light")
        dark = kc.make_qss("dark")
        self.assertNotEqual(light, dark)
        self.assertIn("#ffffff", light)      # canvas
        self.assertIn("#0969da", light)      # accent
        self.assertIn("#d0d7de", light)      # border
        self.assertIn("#1f883d", light)      # success_emphasis
        self.assertIn("#0d1117", dark)       # canvas
        self.assertIn("#2f81f7", dark)       # accent
        self.assertIn("#30363d", dark)       # border
        self.assertIn("#238636", dark)       # success_emphasis

    def test_theme_colors_are_exclusive(self):
        light = kc.make_qss("light")
        dark = kc.make_qss("dark")
        self.assertNotIn("#0d1117", light)
        self.assertNotIn("#2f81f7", light)
        self.assertNotIn("#0969da", dark)
        self.assertNotIn("#d0d7de", dark)

    def test_no_legacy_colors(self):
        for name in kc.THEME_NAMES:
            qss = kc.make_qss(name)
            self.assertNotIn("#89b4fa", qss, name)   # 旧紫色系主色
            self.assertNotIn("#1e1e2e", qss, name)   # 旧深色背景

    def test_invalid_theme_falls_back(self):
        self.assertEqual(kc.make_qss("不存在的主题"), kc.make_qss())
        self.assertEqual(kc.make_qss(None), kc.make_qss())
        self.assertIn(kc.make_qss("不存在的主题"), [kc.make_qss("light"), kc.make_qss("dark")])

    def test_qss_has_no_unsubstituted_tokens(self):
        for name in kc.THEME_NAMES:
            qss = kc.make_qss(name)
            self.assertNotIn("$", qss, name)
            # 主题靠动态属性选择器实现，界面控件不写死颜色
            self.assertIn('QLabel[role="muted"]', qss)
            self.assertIn('QFrame#TaskRow[state="running"]', qss)
            self.assertIn('QLabel[role="status"]', qss)

    def test_theme_toggle_persists_in_config(self):
        before = kc.current_theme()
        with _TempConfig() as path:
            win = kc.MainWindow()
            try:
                win.set_theme("light")
                self.assertEqual(kc.current_theme(), "light")
                win._save()
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("theme"), "light")
                win.set_theme("dark")
                win._save()
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("theme"), "dark")
            finally:
                win.close()
        kc.set_current_theme(before)

    def test_theme_button_text_is_chinese(self):
        with _TempConfig():
            win = kc.MainWindow()
            try:
                win.set_theme("dark")
                self.assertEqual(win.btn_theme.text(), "切换到浅色")
                win.set_theme("light")
                self.assertEqual(win.btn_theme.text(), "切换到深色")
            finally:
                win.close()

    def test_symbolic_theme_never_depends_on_system(self):
        self.assertIn(kc.system_theme(), kc.THEME_NAMES)
        self.assertEqual(kc.set_current_theme("bogus"), kc.current_theme())


# ══════════════════════════════════════════════════════════
#  8. 配置文件位置
# ══════════════════════════════════════════════════════════
class ConfigPathTests(unittest.TestCase):
    def test_env_var_wins(self):
        with _AppDirStub("clicker_config.json") as app_dir:
            # 程序目录里同时存在 clicker_config.json，环境变量依然优先
            target = os.path.join(tempfile.gettempdir(), "kc_env_config.json")
            with _EnvPatch(KEYCLICKER_CONFIG=target):
                self.assertEqual(kc.resolve_config_path(app_dir), target)

    def test_empty_env_var_is_ignored(self):
        with _AppDirStub("clicker_config.json") as app_dir:
            portable = os.path.join(app_dir, "clicker_config.json")
            with _EnvPatch(KEYCLICKER_CONFIG="   "):
                self.assertEqual(kc.resolve_config_path(app_dir), portable)

    def test_existing_portable_config_wins_over_appdata(self):
        with _AppDirStub("clicker_config.json") as app_dir:
            portable = os.path.join(app_dir, "clicker_config.json")
            with _EnvPatch(KEYCLICKER_CONFIG=None, APPDATA=_fake_appdata()):
                self.assertEqual(kc.resolve_config_path(app_dir), portable)

    def test_writable_app_dir_defaults_to_portable(self):
        # v1.2.4 起：程序目录可写就默认把配置放在程序目录（便携优先）
        with _AppDirStub() as app_dir:          # 空目录，但可写
            with _EnvPatch(KEYCLICKER_CONFIG=None, APPDATA=_fake_appdata()):
                self.assertEqual(kc.resolve_config_path(app_dir),
                                 os.path.join(app_dir, "clicker_config.json"))

    def test_readonly_app_dir_falls_back_to_appdata(self):
        with _AppDirStub() as app_dir:
            appdata = _fake_appdata()
            real = kc._dir_writable
            kc._dir_writable = lambda d: False      # 模拟程序目录不可写
            try:
                with _EnvPatch(KEYCLICKER_CONFIG=None, APPDATA=appdata):
                    path = kc.resolve_config_path(app_dir)
            finally:
                kc._dir_writable = real
            self.assertEqual(path, os.path.join(appdata, "KeyClicker", "config.json"))
            self.assertTrue(path.startswith(appdata))   # 绝不落入真实用户目录

    def test_ensure_config_dir_creates_directory(self):
        target = os.path.join(tempfile.gettempdir(), "kc_ensure_dir", "config.json")
        folder = os.path.dirname(target)
        shutil.rmtree(folder, ignore_errors=True)
        try:
            self.assertFalse(os.path.isdir(folder))
            kc.ensure_config_dir(target)
            self.assertTrue(os.path.isdir(folder))
            kc.ensure_config_dir(target)     # 重复调用不报错
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_module_config_path_is_usable(self):
        self.assertIsInstance(kc.CONFIG_PATH, str)
        self.assertTrue(kc.CONFIG_PATH)


# ══════════════════════════════════════════════════════════
#  9. 窗口锁定
# ══════════════════════════════════════════════════════════
class WindowLockTaskTests(unittest.TestCase):
    def test_click_task_blocked_when_window_mismatch(self):
        t = kc.ClickTask(FAKE_KEY, 20, window_match="记事本")
        with _StubForeground("Visual Studio Code"):
            t.start()
            time.sleep(0.15)
            self.assertTrue(t.running)
            self.assertTrue(t.window_blocked)
            self.assertEqual(t.click_count, 0)     # 一个键都不发、不计数
            t.stop()
        self.assertFalse(t.window_blocked)

    def test_click_task_sends_when_window_matched(self):
        t = kc.ClickTask(FAKE_KEY, 20, window_match="记事本")
        with _StubForeground("无标题 - 记事本"):
            t.start()
            time.sleep(0.15)
            self.assertGreaterEqual(t.click_count, 2)
            self.assertFalse(t.window_blocked)
            t.stop()

    def test_click_task_without_window_match_is_never_blocked(self):
        t = kc.ClickTask(FAKE_KEY, 20)
        with _StubForeground("任意窗口"):
            t.start()
            time.sleep(0.1)
            self.assertFalse(t.window_blocked)
            self.assertGreaterEqual(t.click_count, 1)
            t.stop()

    def test_timer_task_blocked_when_window_mismatch(self):
        t = kc.TimerTask([FAKE_KEY], 0, 1, kc.MIN_INTERVAL_MS, window_match="记事本")
        with _StubForeground("计算器"):
            t.start()
            time.sleep(1.4)
            self.assertEqual(t.fire_count, 0)      # 到点不匹配 → 不触发、不计数
            self.assertTrue(t.window_blocked)
            t.stop()

    def test_timer_task_fires_once_window_matched(self):
        t = kc.TimerTask([FAKE_KEY], 0, 1, kc.MIN_INTERVAL_MS, window_match="记事本")
        with _StubForeground("无标题 - 记事本"):
            t.start()
            deadline = time.time() + 3.0
            while t.fire_count < 1 and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(t.fire_count, 1)
            self.assertFalse(t.window_blocked)
            t.stop()

    def test_window_fields_roundtrip(self):
        click = kc.ClickTask("a", 120, window_match="记事本", window_mode="exact")
        d = click.to_dict()
        self.assertEqual(d["window_match"], "记事本")
        self.assertEqual(d["window_mode"], "exact")
        back = kc.ClickTask.from_dict(json.loads(json.dumps(d, ensure_ascii=False)))
        self.assertEqual(back.window_match, "记事本")
        self.assertEqual(back.window_mode, "exact")
        self.assertEqual(back.keys, "a")
        self.assertEqual(back.interval_ms, 120)

        timer = kc.TimerTask(["a", "b"], 0, 5, 100,
                             window_match="浏览器", window_mode="regex")
        d2 = timer.to_dict()
        self.assertEqual(d2["window_match"], "浏览器")
        self.assertEqual(d2["window_mode"], "regex")
        back2 = kc.TimerTask.from_dict(d2)
        self.assertEqual(back2.window_match, "浏览器")
        self.assertEqual(back2.window_mode, "regex")
        self.assertEqual(back2.seq, ["a", "b"])

    def test_legacy_task_dict_without_window_fields(self):
        legacy = {"type": "click", "keys": "win+x", "interval": 50}
        t = kc.ClickTask.from_dict(legacy)
        self.assertEqual(t.window_match, "")
        self.assertEqual(t.window_mode, "contains")
        self.assertFalse(t.window_blocked)

    def test_invalid_window_mode_normalized(self):
        self.assertEqual(kc.ClickTask("a", 100, window_mode="bogus").window_mode,
                         "contains")
        self.assertEqual(kc.TimerTask(["a"], 0, 3, window_mode=None).window_mode,
                         "contains")

    def test_page_roundtrip_keeps_window_fields(self):
        page = kc.Page("配置", [kc.ClickTask("a", 100, window_match="记事本")])
        back = kc.Page.from_dict(json.loads(json.dumps(page.to_dict(),
                                                       ensure_ascii=False)))
        self.assertEqual(back.tasks[0].window_match, "记事本")


# ══════════════════════════════════════════════════════════
#  10. 配置页界面上的窗口匹配
# ══════════════════════════════════════════════════════════
class WindowPanelTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _TempConfig()
        self.cfg.__enter__()

    def tearDown(self):
        self.cfg.__exit__(None, None, None)

    def test_add_task_carries_window_settings(self):
        win = kc.MainWindow()
        try:
            panel = win.panels[0]
            panel.window_edit.setText("记事本")
            panel.window_mode_combo.setCurrentIndex(kc.WINDOW_MODES.index("exact"))
            panel._add_task()
            self.assertEqual(len(panel.page.tasks), 1)
            task = panel.page.tasks[0]
            self.assertEqual(task.window_match, "记事本")
            self.assertEqual(task.window_mode, "exact")
        finally:
            win.close()

    def test_invalid_regex_pattern_is_rejected(self):
        win = kc.MainWindow()
        try:
            panel = win.panels[0]
            panel.window_edit.setText("([")
            panel.window_mode_combo.setCurrentIndex(kc.WINDOW_MODES.index("regex"))
            with _NoDialog() as box:
                panel._add_task()
                self.assertTrue(box.calls)
            self.assertEqual(len(panel.page.tasks), 0)
        finally:
            win.close()

    def test_row_shows_window_state(self):
        win = kc.MainWindow()
        try:
            panel = win.panels[0]
            panel.window_edit.setText("记事本")
            panel._add_task()
            row = panel.rows[0]
            row.refresh()
            self.assertFalse(row.window_lb.isHidden())
            self.assertIn("记事本", row.window_lb.text())
            self.assertEqual(row.property("state"), "stopped")
            self.assertFalse(row.task.window_blocked)
        finally:
            win.close()


# ══════════════════════════════════════════════════════════
#  11. 界面细节（序列区配色 / 字体 / 下拉 hover / 焦点）
# ══════════════════════════════════════════════════════════
class UiPolishTests(unittest.TestCase):
    """v1.2.1 的界面打磨：序列区底色、字体统一、下拉交互、点空白失焦。"""

    def setUp(self):
        self.cfg = _TempConfig()
        self.cfg.__enter__()
        self.win = kc.MainWindow()

    def tearDown(self):
        self.win.close()
        self.cfg.__exit__(None, None, None)

    # ── 配色令牌 ──
    def test_seq_tokens_exist_and_differ_between_themes(self):
        for name in kc.THEME_NAMES:
            theme = kc.THEMES[name]
            for token in ("seq_bg", "seq_border", "seq_fg", "field_bg", "field_hover"):
                self.assertIn(token, theme)
        for token in ("seq_bg", "seq_border", "seq_fg"):
            self.assertNotEqual(kc.THEMES["light"][token], kc.THEMES["dark"][token])

    def test_seq_area_does_not_blend_into_card(self):
        """序列区底色必须和卡片底色不同，否则浅色下整块区域看不出来。"""
        for name in kc.THEME_NAMES:
            theme = kc.THEMES[name]
            self.assertNotEqual(theme["seq_bg"], theme["canvas_subtle"])
            self.assertNotEqual(theme["seq_bg"], theme["canvas"])

    # ── 样式表 ──
    def test_qss_has_timer_group_and_combo_hover(self):
        for name in kc.THEME_NAMES:
            qss = kc.make_qss(name)
            self.assertIn("QWidget#TimerGroup", qss)
            self.assertIn("QComboBox:hover", qss)
            self.assertIn("QComboBox QLineEdit", qss)
            self.assertIn(kc.THEMES[name]["seq_bg"], qss)

    def test_seq_button_uses_main_font(self):
        """「点击设置序列」用中文字体，不能落到等宽字体上造成违和感。"""
        for name in kc.THEME_NAMES:
            qss = kc.make_qss(name)
            start = qss.index("QPushButton#SeqBtn")
            end = qss.index("}", start)
            rule = qss[start:end]
            self.assertIn("font-family", rule)
            self.assertNotIn("Consolas", rule)

    def test_status_labels_use_main_font(self):
        for name in kc.THEME_NAMES:
            qss = kc.make_qss(name)
            for role in ("progress", "capture", "seqbox"):
                start = qss.index('QLabel[role="%s"]' % role)
                end = qss.index("}", start)
                self.assertNotIn("Consolas", qss[start:end])

    # ── 窗口下拉框 ──
    def test_panel_uses_editable_window_combo(self):
        panel = self.win.panels[0]
        self.assertIsInstance(panel.window_edit, kc.WindowCombo)
        self.assertTrue(panel.window_edit.isEditable())

    def test_window_combo_behaves_like_line_edit(self):
        combo = kc.WindowCombo()
        self.assertTrue(combo.isEditable())
        combo.setText("记事本")
        self.assertEqual(combo.text(), "记事本")
        seen = []
        combo.textChanged.connect(seen.append)
        combo.setEditText("计算器")
        self.assertIn("计算器", seen)
        combo.setText(None)
        self.assertEqual(combo.text(), "")

    def test_window_combo_refresh_keeps_typed_text(self):
        combo = kc.WindowCombo()
        combo.setText("我的关键词")
        count = combo.refresh_windows()
        self.assertIsInstance(count, int)
        self.assertGreaterEqual(count, 0)
        self.assertEqual(combo.text(), "我的关键词")

    def test_list_window_titles_shape(self):
        titles = kc.list_window_titles()
        self.assertIsInstance(titles, list)
        self.assertLessEqual(len(titles), 40)
        for t in titles:
            self.assertIsInstance(t, str)
            self.assertEqual(t, t.strip())
            self.assertTrue(t)
        lowered = [t.lower() for t in titles]
        self.assertEqual(len(set(lowered)), len(lowered))   # 已去重
        self.assertEqual(lowered, sorted(lowered))          # 已排序
        self.assertLessEqual(len(kc.list_window_titles(1)), 1)

    def test_grab_window_fills_keyword(self):
        panel = self.win.panels[0]
        old_title = kc.foreground_window_title
        old_list = kc.list_window_titles
        old_self = kc.foreground_window_is_self
        kc.foreground_window_title = lambda: "无标题 - 记事本"
        kc.list_window_titles = lambda limit=40: ["无标题 - 记事本"]
        kc.foreground_window_is_self = lambda: False
        try:
            panel._apply_grabbed_window()       # 倒数结束后的实际抓取动作
        finally:
            kc.foreground_window_title = old_title
            kc.list_window_titles = old_list
            kc.foreground_window_is_self = old_self
        self.assertEqual(panel.window_edit.text(), "无标题 - 记事本")
        self.assertIn("无标题 - 记事本", [panel.window_edit.itemText(i)
                                      for i in range(panel.window_edit.count())])

    # ── 焦点 ──
    def test_blank_click_clears_focus_but_input_click_does_not(self):
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        panel = self.win.panels[0]
        self.win.show()
        QApplication.processEvents()
        panel.click_key.setFocus()
        QApplication.processEvents()
        if QApplication.focusWidget() is not panel.click_key:
            self.skipTest("离屏平台未提供真实键盘焦点")

        def _press(target):
            ev = QMouseEvent(
                QEvent.Type.MouseButtonPress, QPointF(3, 3), QPointF(3, 3),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(target, ev)
            QApplication.processEvents()

        _press(panel.click_key)          # 点到输入框上：焦点保留
        self.assertTrue(panel.click_key.hasFocus())
        _press(panel)                    # 点到空白处：焦点释放
        self.assertFalse(panel.click_key.hasFocus())

    # ── 图标 ──
    def test_app_icon_helper_is_safe(self):
        from PyQt6.QtGui import QIcon

        self.assertIsInstance(kc.app_icon(), QIcon)
        self.assertTrue(kc._resource_path("logo.png").endswith("logo.png"))
        self.assertIsInstance(self.win.windowIcon(), QIcon)
        if not kc.app_icon().isNull():       # 仓库里带了 logo 时必须真的装上
            self.assertFalse(self.win.windowIcon().isNull())


# ══════════════════════════════════════════════════════════
#  12. v1.2.2 界面打磨：圆角下拉 / 可见箭头 / 对勾 / 常驻热键提示
# ══════════════════════════════════════════════════════════
class UiPolish22Tests(unittest.TestCase):
    """v1.2.2：下拉圆角化、自绘箭头、列表项对勾、序列区实线边框、常驻提示。"""

    def setUp(self):
        self.cfg = _TempConfig()
        self.cfg.__enter__()
        self.win = kc.MainWindow()

    def tearDown(self):
        self.win.close()
        self.cfg.__exit__(None, None, None)

    # ── 控件类型 ──
    def test_all_combos_use_themed_widget(self):
        panel = self.win.panels[0]
        for widget in (panel.mode_combo, panel.window_mode_combo,
                       self.win.page_combo, panel.window_edit):
            self.assertIsInstance(widget, kc.ThemedComboBox)
        self.assertIsInstance(panel.window_edit, kc.WindowCombo)

    def test_mode_combo_has_no_fake_radio_prefix(self):
        """「〇 连点」那种假单选圈去掉，改由列表项右侧的对勾表示当前项。"""
        panel = self.win.panels[0]
        texts = [panel.mode_combo.itemText(i) for i in range(panel.mode_combo.count())]
        self.assertEqual(texts, ["连点", "定时序列"])
        self.assertNotIn("〇", "".join(texts))

    def test_combo_uses_rounded_delegate(self):
        panel = self.win.panels[0]
        self.assertIsInstance(panel.mode_combo.itemDelegate(),
                              kc.RoundedItemDelegate)
        self.assertIsInstance(panel.mode_combo.view().itemDelegate(),
                              kc.RoundedItemDelegate)

    # ── 下拉箭头可见 ──
    def test_dropdown_arrow_is_painted(self):
        """箭头必须是自绘的（QSS 把系统箭头压掉了），所以右侧一定有色像素。"""
        for name in kc.THEME_NAMES:
            kc.set_current_theme(name)
            QApplication.instance().setStyleSheet(kc.make_qss(name))
            combo = kc.ThemedComboBox()
            combo.addItems(["包含", "精确", "正则"])
            combo.resize(120, 30)
            combo.show()
            QApplication.processEvents()
            pix = combo.grab()
            img = pix.toImage()
            theme = kc.THEMES[name]
            want = {_rgb_key(theme["accent"]), _rgb_key(theme["fg_muted"]),
                    _rgb_key(theme["fg_subtle"])}
            hits = 0
            for y in range(img.height()):
                for x in range(max(0, img.width() - 26), img.width()):
                    if (int(img.pixel(x, y)) & 0xFFFFFF) in want:
                        hits += 1
            combo.close()
            self.assertGreater(hits, 8, f"{name} 主题下看不到下拉箭头")

    def test_popup_is_frameless_top_level(self):
        """圆角弹层：无边框 + 半透明，且绝不能被误设到主窗口上。"""
        from PyQt6.QtCore import Qt as _Qt

        panel = self.win.panels[0]
        popup = panel.mode_combo.view().window()
        self.assertTrue(popup.isWindow())
        self.assertIsNot(popup, self.win.window())
        self.assertTrue(bool(popup.windowFlags() & _Qt.WindowType.FramelessWindowHint))
        self.assertTrue(popup.testAttribute(
            _Qt.WidgetAttribute.WA_TranslucentBackground))
        # 主窗口不能被牵连
        self.assertFalse(bool(self.win.windowFlags()
                              & _Qt.WindowType.FramelessWindowHint))

    # ── 列表项：圆角高亮 + 对勾 ──
    def test_item_delegate_paints_selection_and_checkmark(self):
        from PyQt6.QtWidgets import QListView

        for name in kc.THEME_NAMES:
            kc.set_current_theme(name)
            QApplication.instance().setStyleSheet(kc.make_qss(name))
            combo = kc.ThemedComboBox()
            combo.addItems(["包含", "精确", "正则"])
            view = QListView()
            view.setItemDelegate(kc.RoundedItemDelegate(combo))
            view.setModel(combo.model())
            view.resize(200, 99)
            index = combo.model().index(0, 0)
            view.setCurrentIndex(index)
            view.selectionModel().select(
                index, view.selectionModel().SelectionFlag.ClearAndSelect)
            view.show()
            QApplication.processEvents()
            img = view.grab().toImage()
            accent = _rgb_key(kc.THEMES[name]["accent"])
            hits = 0
            for y in range(img.height()):
                for x in range(img.width()):
                    if (int(img.pixel(x, y)) & 0xFFFFFF) == accent:
                        hits += 1
            view.close()
            combo.close()
            self.assertGreater(hits, 200, f"{name} 主题下看不出选中项")

    # ── 序列区实线边框 ──
    def test_no_dashed_border_anywhere(self):
        for name in kc.THEME_NAMES:
            self.assertNotIn("dashed", kc.make_qss(name))

    def test_seq_area_uses_strong_solid_border(self):
        for name in kc.THEME_NAMES:
            qss = kc.make_qss(name)
            strong = kc.THEMES[name]["seq_border_strong"]
            self.assertNotEqual(strong, kc.THEMES[name]["seq_border"])
            for selector in ("QWidget#TimerGroup", 'QPushButton#SeqBtn[filled="false"]'):
                start = qss.index(selector)
                rule = qss[start:qss.index("}", start)]
                self.assertIn("solid", rule)
                self.assertIn(strong, rule)

    def test_seq_button_keeps_emphasis_background(self):
        qss = kc.make_qss("light")
        start = qss.index('QPushButton#SeqBtn[filled="false"]')
        rule = qss[start:qss.index("}", start)]
        self.assertIn(kc.THEMES["light"]["seq_bg"], rule)

    # ── 复选框 / 单选框圆角指示器 ──
    def test_checkbox_indicators_are_rounded(self):
        for name in kc.THEME_NAMES:
            qss = kc.make_qss(name)
            self.assertIn("QCheckBox::indicator", qss)
            start = qss.index("QCheckBox::indicator {")
            rule = qss[start:qss.index("}", start)]
            self.assertIn("border-radius: 4px", rule)
            self.assertIn("QRadioButton::indicator", qss)

    # ── 状态栏热键提示常驻 ──
    def test_hotkey_hint_is_permanent_widget(self):
        hint = self.win.hotkey_hint
        self.assertIs(hint.parent(), self.win.statusBar())
        self.assertIn("热键", hint.text())
        before = hint.text()
        self.win.show_status("临时消息", 3000)      # 临时消息不能顶掉常驻提示
        QApplication.processEvents()
        self.assertEqual(hint.text(), before)

    def test_hotkey_hint_reflects_registration_result(self):
        self.win._hotkey_names = []
        self.win._refresh_hotkey_hint()
        self.assertIn("失败", self.win.hotkey_hint.text())
        self.win._hotkey_names = ["Ctrl+` 开始/停止当前页", "F6 全部停止"]
        self.win._refresh_hotkey_hint()
        self.assertIn("F6", self.win.hotkey_hint.text())
        self.assertIn("Ctrl+`", self.win.hotkey_hint.text())

    # ── 抓取前台窗口 ──
    def test_grab_window_starts_countdown(self):
        panel = self.win.panels[0]
        panel._grab_window()
        self.assertIsNotNone(panel._grab_timer)
        self.assertIn("秒", panel.window_grab_btn.text())
        self.assertFalse(panel.window_grab_btn.isEnabled())
        panel._grab_timer.stop()
        panel._set_grab_button(False)

    def test_countdown_end_fills_keyword_and_restores_button(self):
        panel = self.win.panels[0]
        old = (kc.foreground_window_title, kc.list_window_titles,
               kc.foreground_window_is_self)
        kc.foreground_window_title = lambda: "计算器"
        kc.list_window_titles = lambda limit=40: ["计算器", "记事本"]
        kc.foreground_window_is_self = lambda: False
        try:
            panel._grab_window()
            panel._grab_left = 0
            panel._tick_grab_countdown()
        finally:
            (kc.foreground_window_title, kc.list_window_titles,
             kc.foreground_window_is_self) = old
        self.assertEqual(panel.window_edit.text(), "计算器")
        self.assertEqual(panel.window_grab_btn.text(), "抓取当前窗口")
        self.assertTrue(panel.window_grab_btn.isEnabled())

    def test_grab_window_rejects_own_window(self):
        panel = self.win.panels[0]
        panel.window_edit.setText("")
        old = (kc.foreground_window_title, kc.foreground_window_is_self)
        kc.foreground_window_title = lambda: "键盘连点器"
        kc.foreground_window_is_self = lambda: True
        try:
            with _NoDialog() as box:
                panel._apply_grabbed_window()
                self.assertTrue(box.calls)
        finally:
            kc.foreground_window_title, kc.foreground_window_is_self = old
        self.assertEqual(panel.window_edit.text(), "")

    def test_grab_helpers_are_safe(self):
        self.assertIsInstance(kc.foreground_window_is_self(), bool)
        self.assertIsInstance(kc.foreground_window_title(), str)
        self.assertIsInstance(kc.foreground_process_name(), str)

    def test_repolish_accepts_widgets(self):
        panel = self.win.panels[0]
        kc.repolish(panel.seq_btn)
        kc.repolish(panel.mode_combo)
        kc.repolish(None)          # 不允许抛异常


# ══════════════════════════════════════════════════════════
#  13. v1.2.3 修复：下拉弹层「直角白框」→ 真圆角卡片
# ══════════════════════════════════════════════════════════
class PopupCard23Tests(unittest.TestCase):
    """v1.2.3：弹层容器自绘圆角卡片，四角透明，不再出现突兀的方角框。"""

    def setUp(self):
        self.cfg = _TempConfig()
        self.cfg.__enter__()
        self.win = kc.MainWindow()
        self.win.show()
        QApplication.processEvents()

    def tearDown(self):
        self.win.close()
        self.cfg.__exit__(None, None, None)

    # ── 工具 ──
    @staticmethod
    def _apply_theme(name):
        kc.set_current_theme(name)
        QApplication.instance().setStyleSheet(kc.make_qss(name))

    def _popup_image(self, combo):
        """弹出下拉并把弹层窗口截图（含 alpha 通道）。"""
        combo.showPopup()
        QApplication.processEvents()
        img = combo.view().window().grab().toImage()
        combo.hidePopup()
        QApplication.processEvents()
        return img

    def _new_combo(self, items=("包含", "精确", "正则")):
        combo = kc.ThemedComboBox()
        combo.addItems(list(items))
        combo.resize(180, 30)
        combo.show()
        QApplication.processEvents()
        return combo

    # ── 圆角 ──
    def test_popup_corners_are_transparent(self):
        """四角必须是透明像素（= 真圆角）；方角白底会把角填成不透明。"""
        for name in kc.THEME_NAMES:
            self._apply_theme(name)
            combo = self._new_combo()
            img = self._popup_image(combo)
            self.assertGreater(img.width(), 20)
            self.assertGreater(img.height(), 20)
            corners = ((0, 0), (img.width() - 1, 0),
                       (0, img.height() - 1), (img.width() - 1, img.height() - 1))
            for px, py in corners:
                alpha = (int(img.pixel(px, py)) >> 24) & 255
                self.assertLess(alpha, 40, f"{name} 主题弹层四角不是圆角")
            combo.close()

    def test_popup_card_color_follows_theme(self):
        """卡面颜色跟随主题：浅色 #ffffff / 深色 #0d1117。"""
        for name in kc.THEME_NAMES:
            self._apply_theme(name)
            combo = self._new_combo()
            img = self._popup_image(combo)
            center = int(img.pixel(img.width() // 2, 2)) & 0xFFFFFF
            self.assertEqual(center, _rgb_key(kc.THEMES[name]["field_bg"]),
                             f"{name} 主题弹层卡面颜色不对")
            combo.close()

    def test_popup_container_is_rounded_card(self):
        """弹层容器带对象名 / 半透明属性，并挂上圆角卡片过滤器。"""
        combo = self.win.panels[0].mode_combo
        combo.showPopup()
        QApplication.processEvents()
        popup = combo.view().window()
        self.assertEqual(popup.objectName(), "ComboPopup")
        self.assertTrue(popup.testAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(popup.isWindow())
        self.assertIsNot(popup, self.win)
        self.assertIsInstance(combo._popup_filter, kc._PopupCardFilter)
        self.assertIs(combo._popup_filter.parent(), combo)
        combo.hidePopup()

    def test_popup_card_filter_survives_reopen(self):
        """反复弹出后圆角依然生效（防 Qt 重建容器导致失效）。"""
        combo = self.win.panels[0].mode_combo
        for _ in range(3):
            combo.showPopup()
            QApplication.processEvents()
            popup = combo.view().window()
            combo.hidePopup()
            QApplication.processEvents()
        self.assertTrue(popup.testAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertEqual(popup.objectName(), "ComboPopup")
        alpha = (int(popup.grab().toImage().pixel(0, 0)) >> 24) & 255
        self.assertLess(alpha, 40)

    def test_popup_filter_ignores_other_events(self):
        """过滤器只接管 Paint，其它事件必须放行（否则列表交互会坏）。"""
        combo = self.win.panels[0].mode_combo
        flt = combo._popup_filter
        for etype in (QEvent.Type.MouseMove, QEvent.Type.KeyPress,
                      QEvent.Type.Show, QEvent.Type.Hide):
            self.assertFalse(flt.eventFilter(combo, QEvent(etype)))

    def test_popup_filter_is_defensive(self):
        """自绘出错时不能吞掉绘制（回退默认绘制，避免弹层空白）。"""
        combo = self.win.panels[0].mode_combo

        class _Boom(kc._PopupCardFilter):
            def _paint_card(self, widget):
                raise RuntimeError("boom")

        flt = _Boom(combo)
        self.assertFalse(flt.eventFilter(combo, QEvent(QEvent.Type.Paint)))

    def test_popup_scroller_arrows_still_paint(self):
        """长列表的滚动箭头是弹层子控件，不能被圆角卡片覆盖掉。"""
        for name in kc.THEME_NAMES:
            self._apply_theme(name)
            combo = self._new_combo([f"窗口标题 {i} - 记事本" for i in range(30)])
            img = self._popup_image(combo)
            top_band = [int(img.pixel(x, y)) & 0xFFFFFF
                        for y in range(0, min(14, img.height()))
                        for x in range(img.width())]
            card = _rgb_key(kc.THEMES[name]["field_bg"])
            arrowish = [c for c in top_band if c not in (card, 0x000000)]
            self.assertGreater(len(arrowish), 4, f"{name} 主题滚动箭头看不见了")
            combo.close()


# ══════════════════════════════════════════════════════════
#  17. v1.2.4 后台投递（绑定窗口）与三种生效方式
# ══════════════════════════════════════════════════════════
class BackgroundSendTests(unittest.TestCase):
    """「绑定窗口」生效方式：全程打桩，既不真发窗口消息也不真注入按键。

    用户的核心诉求是**不抢前台**也要能把按键送到目标窗口，所以这里除了
    验证投递本身，还专门盯着"绝不能走全局注入"（全局注入会打进前台窗口）。
    """

    def setUp(self):
        self.calls: list = []
        self._orig = (kc._post_message, kc.send_keys, kc.is_window_valid,
                      kc.find_window, kc.foreground_window_pid)
        kc._post_message = self._fake_post
        kc.send_keys = self._fake_inject
        kc.is_window_valid = lambda hwnd: bool(hwnd)
        kc.find_window = lambda *a, **kw: 0
        kc.foreground_window_pid = lambda: 0

    def tearDown(self):
        (kc._post_message, kc.send_keys, kc.is_window_valid,
         kc.find_window, kc.foreground_window_pid) = self._orig

    def _fake_post(self, hwnd, msg, vk, lparam):
        self.calls.append(("post", int(hwnd), int(msg), int(vk), int(lparam)))
        return True

    def _fake_inject(self, keys, hold_ms=20):
        self.calls.append(("inject", tuple(keys)))
        return True

    def _posts(self):
        return [c for c in self.calls if c[0] == "post"]

    def _injects(self):
        return [c for c in self.calls if c[0] == "inject"]

    # ── 常量表 ──
    def test_send_modes_have_labels_and_tips(self):
        self.assertEqual(kc.SEND_MODES, ("foreground", "process", "window"))
        for m in kc.SEND_MODES:
            self.assertIn(m, kc.SEND_MODE_LABELS)
            self.assertIn(m, kc.SEND_MODE_TIPS)
            self.assertTrue(kc.SEND_MODE_TIPS[m].strip())

    def test_no_mode_steals_foreground(self):
        """用户明确要求不抢前台：源码里不许出现切前台的调用。"""
        with open(os.path.join(ROOT, "keyclicker.py"), encoding="utf-8") as f:
            src = f.read()
        for bad in ("SetForegroundWindow", "SetActiveWindow", "BringWindowToTop",
                    "AttachThreadInput", "ShowWindow", "keybd_event"):
            self.assertNotIn(bad, src, f"不应调用 {bad}：那会抢走前台窗口")

    # ── post_keys 本身 ──
    def test_post_keys_combo_order(self):
        """组合键：先按修饰键 → 再按主键 → 逆序松开。"""
        kc.post_keys(777, ["ctrl", "a"], hold_ms=8)
        posts = self._posts()
        self.assertEqual([c[2] for c in posts],
                         [kc.WM_KEYDOWN, kc.WM_KEYDOWN,
                          kc.WM_KEYUP, kc.WM_KEYUP])
        vks = [c[3] for c in posts]
        self.assertEqual(vks[0], vks[3])      # ctrl 先按下、最后松开
        self.assertEqual(vks[1], vks[2])      # a 后按下、先松开
        self.assertIn(vks[0], (0x11, 0xA2, 0xA3))   # VK_CONTROL / 左右 Ctrl
        self.assertEqual(vks[1], 0x41)              # VK_A
        self.assertNotEqual(vks[0], vks[1])

    def test_post_keys_alt_uses_syskey(self):
        """含 Alt 的组合键要走 WM_SYSKEYDOWN/UP，否则很多程序收不到。"""
        kc.post_keys(777, ["alt", "f4"], hold_ms=8)
        self.assertEqual({c[2] for c in self._posts()},
                         {kc.WM_SYSKEYDOWN, kc.WM_SYSKEYUP})

    def test_post_keys_lparam_marks_keyup(self):
        kc.post_keys(777, ["a"], hold_ms=8)
        down, up = self._posts()
        self.assertFalse(down[4] & (1 << 31))
        self.assertTrue(up[4] & (1 << 31))
        self.assertTrue(up[4] & (1 << 30))
        self.assertEqual(down[4] & 0xFF, 1)   # 重复次数 = 1

    def test_post_keys_rejects_bad_input(self):
        self.assertFalse(kc.post_keys(0, ["a"]))          # 没有窗口
        self.assertFalse(kc.post_keys(777, []))           # 没有按键
        self.assertFalse(kc.post_keys(777, [FAKE_KEY]))   # 键名无法识别
        self.assertEqual(self.calls, [])

    def test_post_keys_refuses_invalid_window(self):
        kc.is_window_valid = lambda hwnd: False
        self.assertFalse(kc.post_keys(777, ["a"]))
        self.assertEqual(self.calls, [])

    # ── 三种生效方式的分流 ──
    def test_window_mode_posts_and_never_injects(self):
        """绑定窗口：走窗口消息投递，绝不能退化成全局注入。"""
        t = kc.ClickTask("a", 100, send_mode="window")
        t.bind_target(hwnd=4242, pid=77, exe="game.exe")
        self.assertTrue(t.is_bound)
        self.assertTrue(t._send(["a"]))
        self.assertTrue(self._posts())
        self.assertFalse(self._injects(), "绑定窗口模式不该走全局注入")
        self.assertTrue(all(c[1] == 4242 for c in self._posts()))
        self.assertEqual({c[2] for c in self._posts()},
                         {kc.WM_KEYDOWN, kc.WM_KEYUP})

    def test_window_mode_without_target_sends_nothing(self):
        t = kc.ClickTask("a", 100, send_mode="window")
        self.assertFalse(t.is_bound)
        self.assertFalse(t._send(["a"]))
        self.assertEqual(self.calls, [])

    def test_window_mode_ignores_foreground_title(self):
        """目标窗口在后台、前台是毫不相干的程序，照样允许发送。"""
        t = kc.ClickTask("a", 100, send_mode="window")
        t.bind_target(hwnd=555, pid=9, exe="x.exe")
        with _StubForeground("完全无关的窗口"):
            self.assertTrue(t._window_allows())

    def test_foreground_and_process_modes_use_global_injection(self):
        for mode in ("foreground", "process"):
            self.calls.clear()
            t = kc.ClickTask("a", 100, send_mode=mode)
            self.assertTrue(t._send(["a"]))
            self.assertEqual(len(self._injects()), 1, mode)
            self.assertFalse(self._posts(), mode)

    def test_invalid_send_mode_falls_back_to_foreground(self):
        self.assertEqual(kc.ClickTask("a", 100, send_mode="bogus").send_mode,
                         "foreground")
        self.assertEqual(kc.TimerTask(["a"], 0, 3, send_mode=None).send_mode,
                         "foreground")

    # ── 目标解析 / 进程绑定 ──
    def test_bind_target_resets_cached_handle(self):
        t = kc.ClickTask("a", 100, send_mode="window")
        t.bind_target(hwnd=111, pid=1, exe="a.exe")
        self.assertEqual(t._resolve_target(), 111)
        t.bind_target(hwnd=222, pid=2, exe="b.exe")
        self.assertEqual(t._cached_hwnd, 0)          # 换绑必须清缓存
        self.assertEqual(t._resolve_target(), 222)

    def test_resolve_target_refinds_by_process_after_handle_dies(self):
        """目标程序重开后句柄失效 → 按「同进程 + 标题」重新找到新窗口。"""
        seen = {}

        def fake_find(pattern, mode, pid=0, exe="", windows=None):
            seen.update(pattern=pattern, mode=mode, pid=pid, exe=exe)
            return 999

        t = kc.ClickTask("a", 100, send_mode="window", window_match="游戏",
                         window_mode="contains")
        t.bind_target(hwnd=111, pid=1, exe="a.exe")
        kc.find_window = fake_find
        kc.is_window_valid = lambda hwnd: hwnd != 111        # 旧句柄已失效
        self.assertEqual(t._resolve_target(), 999)
        self.assertEqual(seen["pid"], 1)             # 认准绑定的进程
        self.assertEqual(seen["exe"], "a.exe")
        self.assertEqual(seen["pattern"], "游戏")

    # ── 存盘往返 ──
    def test_send_mode_and_target_roundtrip(self):
        t = kc.ClickTask("a", 100, send_mode="window")
        t.bind_target(hwnd=1, pid=2, exe="g.exe")
        d = t.to_dict()
        self.assertEqual(d["send_mode"], "window")
        self.assertEqual((d["target_hwnd"], d["target_pid"], d["target_exe"]),
                         (1, 2, "g.exe"))
        back = kc.ClickTask.from_dict(json.loads(json.dumps(d,
                                                            ensure_ascii=False)))
        self.assertEqual(back.send_mode, "window")
        self.assertEqual((back.target_hwnd, back.target_pid, back.target_exe),
                         (1, 2, "g.exe"))

    def test_legacy_page_without_send_mode(self):
        legacy = {"type": "click", "keys": "a", "interval": 50}
        t = kc.ClickTask.from_dict(legacy)
        self.assertEqual(t.send_mode, "foreground")
        self.assertFalse(t.is_bound)


if __name__ == "__main__":
    unittest.main(verbosity=2)
