# -*- coding: utf-8 -*-
"""TTS翻譯（Windows / Win11）

功能：
- 常駐工作列（系統匣）小工具
- 監聽 Ctrl+C（全域）後，於滑鼠旁顯示「翻譯」與「播放」按鈕
- 自動判斷中/英文（譯向與語音各自獨立）
- 左鍵點擊系統匣圖示：開啟設定（選擇中/英文語音、啟用/停用、結束程式）
- 預設為不啟用

主要依賴：
- PySide6（UI / tray）
- pynput（全域快捷鍵 Ctrl+C）
- pywin32（SAPI：列舉與使用 Windows 系統語音）

翻譯服務：
- translate.googleapis.com 的 gtx 端點（非官方，可能受網路/地區影響）
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

try:
    from pynput import keyboard  # type: ignore
except Exception:  # pragma: no cover
    keyboard = None  # type: ignore

try:
    import win32com.client  # type: ignore
except Exception:  # pragma: no cover
    win32com = None  # type: ignore


APP_TITLE = "TTS翻譯"
DEFAULT_ENABLED = False
DEFAULT_POPUP_AUTO_HIDE_MS = 12000
DEFAULT_MAX_CHARS = 500
DEFAULT_TRANSLATE_TIMEOUT_SEC = 6.0

ACCENT = "#72e3fd"
BG = "#0f1117"
SURFACE = "#151925"
BORDER = "rgba(114,227,253,0.35)"
TEXT = "rgba(255,255,255,0.92)"
MUTED = "rgba(255,255,255,0.68)"

# user_data（與 app_main.py 同層）
APP_DIR = Path(__file__).resolve().parent
USER_DATA = APP_DIR / "user_data"
LOG_DIR = USER_DATA / "logs"
SETTINGS_PATH = USER_DATA / "settings.json"


# -----------------------------
# 設定 / IO
# -----------------------------
@dataclass
class Settings:
    enabled: bool = DEFAULT_ENABLED
    # SAPI voice token id（字串）；留空 => 自動挑第一個符合語系的聲音
    voice_en_id: str = ""
    voice_zh_id: str = ""
    popup_auto_hide_ms: int = DEFAULT_POPUP_AUTO_HIDE_MS
    max_chars: int = DEFAULT_MAX_CHARS
    translate_timeout_sec: float = DEFAULT_TRANSLATE_TIMEOUT_SEC


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Settings:
        with self._lock:
            try:
                if not self.path.exists():
                    return Settings()
                data = json.loads(self.path.read_text(encoding="utf-8"))
                s = Settings()
                for k, v in data.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
                return s
            except Exception:
                return Settings()

    def save(self, s: Settings) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.info("=== %s 啟動 ===", APP_TITLE)


# -----------------------------
# 中英判斷 / 翻譯
# -----------------------------
_RE_ZH = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_RE_EN = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """回傳 "zh" 或 "en"。

    - 若中文字元比例較高 => zh
    - 否則 => en
    """
    t = (text or "").strip()
    if not t:
        return "en"
    zh = len(_RE_ZH.findall(t))
    en = len(_RE_EN.findall(t))
    if zh == 0 and en == 0:
        return "en"
    return "zh" if zh >= en else "en"


class GoogleTranslator:
    """translate.googleapis.com 非官方端點（與你的 JS 版一致 client=gtx）。"""

    def __init__(self):
        self._cache: Dict[Tuple[str, str, str], str] = {}
        self._lock = threading.Lock()

    def translate(self, text: str, sl: str, tl: str, timeout_sec: float) -> str:
        key = (sl, tl, text)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

        q = urllib.parse.quote(text)
        url = (
            "https://translate.googleapis.com/translate_a/single"
            f"?client=gtx&sl={urllib.parse.quote(sl)}&tl={urllib.parse.quote(tl)}&dt=t&q={q}"
        )

        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            translated = ""
            if isinstance(data, list) and data and isinstance(data[0], list):
                translated = "".join(
                    (seg[0] or "")
                    for seg in data[0]
                    if isinstance(seg, list) and seg
                )
            translated = translated.strip()
        except Exception as e:
            logging.warning("translate failed: %s", e)
            translated = ""

        with self._lock:
            self._cache[key] = translated
        return translated


# -----------------------------
# Windows SAPI（語音）
# -----------------------------
@dataclass(frozen=True)
class VoiceInfo:
    token_id: str
    description: str
    group: str  # "en" / "zh" / "other"


def _parse_sapi_language_attr(lang_attr: str) -> List[int]:
    """SAPI token Language attribute 常見格式："409" / "404" / "409;809"。

    以 16 進位解析成 LCID int。
    """
    out: List[int] = []
    for part in (lang_attr or "").split(";"):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(int(s, 16))
        except ValueError:
            continue
    return out


def _classify_lcids(lcids: List[int]) -> str:
    # Windows Language ID 低位 byte：0x09 英文，0x04 中文（含繁簡）
    for lcid in lcids:
        if (lcid & 0xFF) == 0x04:
            return "zh"
    for lcid in lcids:
        if (lcid & 0xFF) == 0x09:
            return "en"
    return "other"


class SapiVoiceManager:
    @staticmethod
    def list_voices() -> List[VoiceInfo]:
        if win32com is None:
            return []
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = voice.GetVoices()
            infos: List[VoiceInfo] = []
            for i in range(tokens.Count):
                tok = tokens.Item(i)
                try:
                    token_id = tok.Id  # type: ignore[attr-defined]
                except Exception:
                    token_id = ""
                try:
                    desc = tok.GetDescription()  # type: ignore[attr-defined]
                except Exception:
                    desc = f"Voice {i}"
                try:
                    lang_attr = tok.GetAttribute("Language")  # type: ignore[attr-defined]
                except Exception:
                    lang_attr = ""
                group = _classify_lcids(_parse_sapi_language_attr(lang_attr))
                infos.append(VoiceInfo(token_id=token_id, description=desc, group=group))
            return infos
        except Exception as e:
            logging.warning("list_voices failed: %s", e)
            return []


class SapiTTSWorker(threading.Thread):
    """以背景 thread 執行 SAPI Speak，避免阻塞 UI。"""

    def __init__(self):
        super().__init__(daemon=True)
        self._q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._stop_evt = threading.Event()
        self._voice = None
        self._token_map: Dict[str, Any] = {}

    def run(self) -> None:
        if win32com is None:
            logging.error("pywin32 不可用，無法使用 Windows 語音。")
            return
        try:
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = self._voice.GetVoices()
            self._token_map = {}
            for i in range(tokens.Count):
                tok = tokens.Item(i)
                try:
                    tid = tok.Id
                except Exception:
                    tid = ""
                if tid:
                    self._token_map[tid] = tok
        except Exception as e:
            logging.error("SAPI init failed: %s", e)
            return

        while not self._stop_evt.is_set():
            try:
                cmd, payload = self._q.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                if cmd == "STOP":
                    self._purge()
                elif cmd == "SPEAK":
                    text, voice_token_id = payload
                    self._speak(text, voice_token_id)
                elif cmd == "QUIT":
                    self._purge()
                    break
            except Exception as e:
                logging.warning("TTS worker error: %s", e)

        self._stop_evt.set()

    def _purge(self) -> None:
        # SVSFPurgeBeforeSpeak = 2
        try:
            if self._voice is not None:
                self._voice.Speak("", 2)
        except Exception:
            pass

    def _speak(self, text: str, voice_token_id: str) -> None:
        if not text.strip() or self._voice is None:
            return
        self._purge()

        tok = self._token_map.get(voice_token_id) if voice_token_id else None
        if tok is not None:
            try:
                self._voice.Voice = tok
            except Exception:
                pass
        try:
            self._voice.Speak(text, 0)  # 同步（在背景 thread 內）
        except Exception as e:
            logging.warning("Speak failed: %s", e)

    def speak(self, text: str, voice_token_id: str) -> None:
        self._q.put(("SPEAK", (text, voice_token_id)))

    def stop(self) -> None:
        self._q.put(("STOP", None))

    def quit(self) -> None:
        self._q.put(("QUIT", None))
        self._stop_evt.set()


# -----------------------------
# UI：Popup（滑鼠旁）
# -----------------------------
class PopupWidget(QtWidgets.QWidget):
    play_clicked = QtCore.Signal()
    translate_clicked = QtCore.Signal()
    closed = QtCore.Signal()

    def __init__(self):
        super().__init__(
            None,
            QtCore.Qt.Tool
            | QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

        self._text = ""
        self._translated = ""

        self._auto_hide_timer = QtCore.QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self.hide_and_emit)

        self._build_ui()

    def _build_ui(self) -> None:
        root = QtWidgets.QFrame()
        root.setObjectName("popupRoot")

        self.btn_translate = QtWidgets.QToolButton()
        self.btn_translate.setObjectName("btnTranslate")
        self.btn_translate.setToolTip("重新翻譯")
        self.btn_translate.setText("譯")
        self.btn_translate.clicked.connect(self.translate_clicked.emit)

        self.btn_play = QtWidgets.QToolButton()
        self.btn_play.setObjectName("btnPlay")
        self.btn_play.setToolTip("播放原文")
        self.btn_play.setText("播")
        self.btn_play.clicked.connect(self.play_clicked.emit)

        self.btn_close = QtWidgets.QToolButton()
        self.btn_close.setObjectName("btnClose")
        self.btn_close.setToolTip("關閉")
        self.btn_close.setText("×")
        self.btn_close.clicked.connect(self.hide_and_emit)

        self.lbl_src = QtWidgets.QLabel()
        self.lbl_src.setObjectName("lblSrc")
        self.lbl_src.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.lbl_tr = QtWidgets.QLabel()
        self.lbl_tr.setObjectName("lblTr")
        self.lbl_tr.setWordWrap(True)
        self.lbl_tr.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        btns = QtWidgets.QHBoxLayout()
        btns.setContentsMargins(0, 0, 0, 0)
        btns.setSpacing(8)
        btns.addWidget(self.btn_translate)
        btns.addWidget(self.btn_play)
        btns.addStretch(1)
        btns.addWidget(self.btn_close)

        lay = QtWidgets.QVBoxLayout(root)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)
        lay.addLayout(btns)
        lay.addWidget(self.lbl_src)
        lay.addWidget(self.lbl_tr)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.addWidget(root)

        self.setStyleSheet(self._style_sheet())

    def _style_sheet(self) -> str:
        return f"""
        #popupRoot {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 14px;
        }}
        QLabel {{
            color: {TEXT};
            font-size: 13px;
        }}
        #lblSrc {{
            color: {MUTED};
            font-size: 12px;
        }}
        QToolButton {{
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 10px;
            padding: 6px 10px;
            color: {TEXT};
            font-size: 12px;
        }}
        QToolButton:hover {{
            border-color: {ACCENT};
            background: rgba(114,227,253,0.12);
        }}
        #btnClose {{
            border-radius: 999px;
            padding: 4px 8px;
        }}
        """

    def show_near_cursor(self, text: str, translated: str, auto_hide_ms: int) -> None:
        self._text = text
        self._translated = translated

        src_preview = text.strip().replace("\n", " ")
        if len(src_preview) > 80:
            src_preview = src_preview[:80] + "…"

        self.lbl_src.setText(src_preview)
        self.lbl_tr.setText(translated if translated else "翻譯中…")

        self.adjustSize()
        pos = QtGui.QCursor.pos()

        x = pos.x() + 16
        y = pos.y() + 18

        screen = QtGui.QGuiApplication.screenAt(pos) or QtGui.QGuiApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            if x + self.width() > geo.right():
                x = max(geo.left(), geo.right() - self.width())
            if y + self.height() > geo.bottom():
                y = max(geo.top(), geo.bottom() - self.height())

        self.move(x, y)
        self.show()
        self._auto_hide_timer.start(max(500, int(auto_hide_ms)))

    def update_translation(self, translated: str) -> None:
        self._translated = translated
        self.lbl_tr.setText(translated if translated else "（翻譯失敗或被阻擋）")
        self.adjustSize()

    def hide_and_emit(self) -> None:
        self.hide()
        self.closed.emit()

    @property
    def text(self) -> str:
        return self._text


# -----------------------------
# UI：設定視窗
# -----------------------------
class SettingsDialog(QtWidgets.QDialog):
    settings_changed = QtCore.Signal(Settings)
    request_quit = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget, store: SettingsStore, sapi_voices: List[VoiceInfo]):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_TITLE}｜設定")
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setMinimumWidth(520)

        self._store = store
        self._voices = sapi_voices
        self._s = store.load()

        self._build_ui()
        self._apply_theme()
        self._load_to_ui()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            f"""
            QDialog {{ background: {BG}; color: {TEXT}; }}
            QLabel {{ color: {TEXT}; }}
            QGroupBox {{
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
                margin-top: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: {MUTED};
            }}
            QComboBox, QSpinBox, QDoubleSpinBox {{
                background: {SURFACE};
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 10px;
                padding: 6px 10px;
            }}
            QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {ACCENT}; }}
            QPushButton {{
                background: rgba(114,227,253,0.12);
                border: 1px solid rgba(114,227,253,0.35);
                border-radius: 10px;
                padding: 8px 12px;
            }}
            QPushButton:hover {{ background: rgba(114,227,253,0.18); border-color: {ACCENT}; }}
            """
        )

    def _build_ui(self) -> None:
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        self.chk_enabled = QtWidgets.QCheckBox("啟用（監聽 Ctrl+C 並顯示翻譯/播放）")
        lay.addWidget(self.chk_enabled)

        grp_voice = QtWidgets.QGroupBox("語音")
        vlay = QtWidgets.QFormLayout(grp_voice)
        vlay.setContentsMargins(12, 12, 12, 12)
        vlay.setSpacing(10)

        self.cmb_en = QtWidgets.QComboBox()
        self.cmb_zh = QtWidgets.QComboBox()

        vlay.addRow("英文語音（en-*）：", self.cmb_en)
        vlay.addRow("中文語音（zh-*）：", self.cmb_zh)

        lay.addWidget(grp_voice)

        grp_misc = QtWidgets.QGroupBox("顯示 / 翻譯")
        mlay = QtWidgets.QFormLayout(grp_misc)
        mlay.setContentsMargins(12, 12, 12, 12)
        mlay.setSpacing(10)

        self.spn_hide = QtWidgets.QSpinBox()
        self.spn_hide.setRange(1000, 60000)
        self.spn_hide.setSingleStep(1000)
        self.spn_hide.setSuffix(" ms")

        self.spn_max = QtWidgets.QSpinBox()
        self.spn_max.setRange(50, 5000)
        self.spn_max.setSingleStep(50)

        self.dsp_timeout = QtWidgets.QDoubleSpinBox()
        self.dsp_timeout.setRange(1.0, 30.0)
        self.dsp_timeout.setSingleStep(0.5)
        self.dsp_timeout.setSuffix(" 秒")

        mlay.addRow("彈窗自動關閉：", self.spn_hide)
        mlay.addRow("最長字數：", self.spn_max)
        mlay.addRow("翻譯逾時：", self.dsp_timeout)

        lay.addWidget(grp_misc)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)

        self.btn_save = QtWidgets.QPushButton("儲存")
        self.btn_save.clicked.connect(self._on_save)

        self.btn_quit = QtWidgets.QPushButton("結束程式")
        self.btn_quit.clicked.connect(self.request_quit.emit)

        self.btn_close = QtWidgets.QPushButton("關閉")
        self.btn_close.clicked.connect(self.close)

        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_close)
        btn_row.addWidget(self.btn_quit)
        lay.addLayout(btn_row)

        self._populate_voices()

    def _populate_voices(self) -> None:
        self.cmb_en.clear()
        self.cmb_zh.clear()

        en = [v for v in self._voices if v.group == "en"]
        zh = [v for v in self._voices if v.group == "zh"]
        if not en:
            en = self._voices[:]
        if not zh:
            zh = self._voices[:]

        def add_items(cmb: QtWidgets.QComboBox, items: List[VoiceInfo]) -> None:
            cmb.addItem("（自動選擇）", userData="")
            for v in items:
                cmb.addItem(v.description, userData=v.token_id)

        add_items(self.cmb_en, en)
        add_items(self.cmb_zh, zh)

    def _load_to_ui(self) -> None:
        s = self._s
        self.chk_enabled.setChecked(bool(s.enabled))
        self.spn_hide.setValue(int(s.popup_auto_hide_ms))
        self.spn_max.setValue(int(s.max_chars))
        self.dsp_timeout.setValue(float(s.translate_timeout_sec))

        def set_combo(cmb: QtWidgets.QComboBox, token_id: str) -> None:
            idx = cmb.findData(token_id)
            cmb.setCurrentIndex(idx if idx >= 0 else 0)

        set_combo(self.cmb_en, s.voice_en_id)
        set_combo(self.cmb_zh, s.voice_zh_id)

    def _on_save(self) -> None:
        s = Settings(
            enabled=self.chk_enabled.isChecked(),
            voice_en_id=str(self.cmb_en.currentData() or ""),
            voice_zh_id=str(self.cmb_zh.currentData() or ""),
            popup_auto_hide_ms=int(self.spn_hide.value()),
            max_chars=int(self.spn_max.value()),
            translate_timeout_sec=float(self.dsp_timeout.value()),
        )
        self._store.save(s)
        self._s = s
        self.settings_changed.emit(s)


# -----------------------------
# 主程式：tray / hotkey / clipboard / popup
# -----------------------------
class HotkeyWatcher(QtCore.QObject):
    """使用 pynput 監聽 Ctrl+C（全域）。"""

    ctrl_c = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._listener = None
        self._ctrl_down = False

    def start(self) -> None:
        if keyboard is None:
            logging.error("pynput 不可用，無法全域監聽 Ctrl+C。")
            return

        def on_press(key):
            try:
                if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                    self._ctrl_down = True
                elif self._ctrl_down:
                    if hasattr(key, "char") and key.char and key.char.lower() == "c":
                        self.ctrl_c.emit()
            except Exception:
                pass

        def on_release(key):
            try:
                if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                    self._ctrl_down = False
            except Exception:
                pass

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()
        logging.info("HotkeyWatcher started")

    def stop(self) -> None:
        try:
            if self._listener:
                self._listener.stop()
        except Exception:
            pass


class AppController(QtCore.QObject):
    # [PATCH] fix-translate-stuck: use-signal
    translation_ready = QtCore.Signal(str, str)
    def __init__(self, app: QtWidgets.QApplication):
        super().__init__()
        self.app = app
        self.store = SettingsStore(SETTINGS_PATH)
        self.settings = self.store.load()

        self.translator = GoogleTranslator()
        self.popup = PopupWidget()
        self.popup.play_clicked.connect(self.on_play_clicked)
        self.popup.translate_clicked.connect(self.on_translate_clicked)

        self.translation_ready.connect(self.on_translation_ready)
        self._last_ctrl_c_ts = 0.0
        self._last_shown_text = ""
        self._last_shown_ts = 0.0

        self.hotkeys = HotkeyWatcher()
        self.hotkeys.ctrl_c.connect(self.on_ctrl_c)
        self.hotkeys.start()

        self.clipboard = self.app.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

        self._tts_worker = SapiTTSWorker()
        self._tts_worker.start()

        self._settings_dialog: Optional[SettingsDialog] = None
        self._tray = self._create_tray()

    def _create_tray(self) -> QtWidgets.QSystemTrayIcon:
        tray = QtWidgets.QSystemTrayIcon(self._make_tray_icon(), self.app)
        tray.setToolTip(APP_TITLE)

        menu = QtWidgets.QMenu()
        self.act_enabled = QtGui.QAction("啟用", menu)
        self.act_enabled.setCheckable(True)
        self.act_enabled.setChecked(bool(self.settings.enabled))
        self.act_enabled.toggled.connect(self.set_enabled)

        act_settings = QtGui.QAction("設定…", menu)
        act_settings.triggered.connect(self.open_settings)

        act_quit = QtGui.QAction("結束", menu)
        act_quit.triggered.connect(self.quit)

        menu.addAction(self.act_enabled)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addSeparator()
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self.on_tray_activated)
        tray.show()
        return tray

    def _make_tray_icon(self) -> QtGui.QIcon:
        size = 64
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(21, 25, 37, 255))
        p.drawEllipse(4, 4, size - 8, size - 8)

        pen = QtGui.QPen(QtGui.QColor(114, 227, 253, 255))
        pen.setWidth(4)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawEllipse(12, 12, size - 24, size - 24)

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(114, 227, 253, 255))
        p.drawEllipse(size // 2 - 6, size // 2 - 6, 12, 12)

        p.end()
        return QtGui.QIcon(pm)

    def on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            self.open_settings()

    def open_settings(self) -> None:
        voices = SapiVoiceManager.list_voices()
        if not self._settings_dialog:
            dlg = SettingsDialog(None, self.store, voices)
            dlg.settings_changed.connect(self.on_settings_changed)
            dlg.request_quit.connect(self.quit)
            self._settings_dialog = dlg
        else:
            # 重新載入聲音清單與設定
            self._settings_dialog._voices = voices  # noqa: SLF001
            self._settings_dialog._populate_voices()  # noqa: SLF001
            self._settings_dialog._s = self.store.load()  # noqa: SLF001
            self._settings_dialog._load_to_ui()  # noqa: SLF001

        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def on_settings_changed(self, s: Settings) -> None:
        self.settings = s
        self.act_enabled.setChecked(bool(s.enabled))
        logging.info("settings updated: enabled=%s", s.enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.settings.enabled = bool(enabled)
        self.store.save(self.settings)
        logging.info("enabled set to %s", enabled)

    def on_ctrl_c(self) -> None:
        self._last_ctrl_c_ts = time.monotonic()

    def on_clipboard_changed(self) -> None:
        if not self.settings.enabled:
            return
        # [PATCH] disable-ctrlc-gate: clipboard-only trigger
        # 原本的 Ctrl+C 時窗 gate 已停用，改為只要剪貼簿文字變更就觸發。

        text = (self.clipboard.text() or "").strip()
        if not text:
            return
        if len(text) > int(self.settings.max_chars):
            return

        now = time.monotonic()
        if text == self._last_shown_text and (now - self._last_shown_ts) < 0.8:
            return

        self._last_shown_text = text
        self._last_shown_ts = now
        self.show_popup(text)

    def show_popup(self, text: str) -> None:
        self.popup.show_near_cursor(text=text, translated="", auto_hide_ms=self.settings.popup_auto_hide_ms)

        src_lang = detect_lang(text)
        if src_lang == "en":
            sl, tl = "en", "zh-TW"
        else:
            sl, tl = "zh-TW", "en"

        th = threading.Thread(
            target=self._translate_and_update,
            args=(text, sl, tl, float(self.settings.translate_timeout_sec)),
            daemon=True,
        )
        th.start()

    def _translate_and_update(self, text: str, sl: str, tl: str, timeout: float) -> None:
        translated = self.translator.translate(text, sl=sl, tl=tl, timeout_sec=timeout)
        # 使用 Qt signal（跨 thread queued）回到 GUI thread 更新 UI
        self.translation_ready.emit(text, translated)

    @QtCore.Slot(str, str)
    def on_translation_ready(self, text: str, translated: str) -> None:
        if self.popup.isVisible() and self.popup.text == text:
            self.popup.update_translation(translated)

    def on_translate_clicked(self) -> None:
        if self.popup.text:
            self.show_popup(self.popup.text)

    def on_play_clicked(self) -> None:
        text = self.popup.text.strip()
        if not text:
            return

        src_lang = detect_lang(text)
        voice_id = self.settings.voice_zh_id if src_lang == "zh" else self.settings.voice_en_id
        self._tts_worker.speak(text, voice_id)

    def quit(self) -> None:
        try:
            self.popup.hide()
        except Exception:
            pass
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        try:
            self._tts_worker.quit()
        except Exception:
            pass
        try:
            self._tray.hide()
        except Exception:
            pass
        logging.info("=== %s 結束 ===", APP_TITLE)
        self.app.quit()


def _ensure_dirs() -> None:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _guard_dependencies() -> Optional[str]:
    if keyboard is None:
        return "缺少 pynput（無法監聽 Ctrl+C）"
    if win32com is None:
        return "缺少 pywin32（無法使用 Windows 語音）"
    return None


def main() -> int:
    _ensure_dirs()
    setup_logging()

    err = _guard_dependencies()
    if err:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QtWidgets.QMessageBox.critical(None, APP_TITLE, err + "\n\n請依 README 安裝相依套件後再執行。")
        return 1

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)

    try:
        QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    _ = AppController(app)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
