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

    def test_portable_flag(self):
        with _AppDirStub("portable.flag") as app_dir:
            with _EnvPatch(KEYCLICKER_CONFIG=None, APPDATA=_fake_appdata()):
                self.assertEqual(kc.resolve_config_path(app_dir),
                                 os.path.join(app_dir, "clicker_config.json"))

    def test_default_appdata_path(self):
        with _AppDirStub() as app_dir:          # 空目录：两个便携条件都不成立
            appdata = _fake_appdata()
            with _EnvPatch(KEYCLICKER_CONFIG=None, APPDATA=appdata):
                path = kc.resolve_config_path(app_dir)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
