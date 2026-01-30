# -*- coding: utf-8 -*-
"""
TTS翻譯（Windows / Win11）

重點：
- 彈窗只顯示「中文翻譯」+「播放/停止」按鈕（不顯示英文原文）
- 朗讀內容為「原文」：原文中文念中文、原文英文念英文（自動偵測）
- 點擊非彈窗區域關閉；滾輪也會關閉（需 pynput）
- 設定可選：字體大小、每行最多字數（超過硬換行）、彈窗位置（滑鼠旁邊 / 滑鼠所在視窗正中心）
- 啟用狀態每次啟動固定「不啟用」，且不記住上次狀態（enabled 不落盤、不讀盤）
- 修正「字體大小儲存後回到 24」：
  1) SettingsStore.save() 改為 merge 寫入（避免被其他流程洗掉欄位）
  2) set_enabled() 不會寫 settings.json
  3) Popup 字體套用使用 pixelSize + QLabel 專屬 QSS font-size(px) 兩道保險
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

# pynput：全域滑鼠監聽（點擊/滾輪關閉彈窗）
try:
    from pynput import mouse, keyboard  # type: ignore
except Exception:
    mouse = None  # type: ignore
    keyboard = None  # type: ignore

# pywin32：Windows SAPI TTS
try:
    import win32com.client  # type: ignore
    import pythoncom  # type: ignore
except Exception:
    win32com = None  # type: ignore
    pythoncom = None  # type: ignore

# pywin32：取得滑鼠所在視窗位置（視窗正中心模式）
try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
except Exception:
    win32gui = None  # type: ignore
    win32con = None  # type: ignore


APP_TITLE = "TTS翻譯"

# 每次啟動都必須預設不啟用（且不記住上次）
DEFAULT_ENABLED_RUNTIME = False

DEFAULT_MAX_CHARS = 500
DEFAULT_TRANSLATE_TIMEOUT_SEC = 6.0

DEFAULT_FONT_SIZE = 24
DEFAULT_MAX_CHARS_PER_LINE = 18

# 彈窗位置模式
POS_CURSOR = "cursor"
POS_SCREEN_CENTER = "screen_center"
DEFAULT_POSITION_MODE = POS_CURSOR

# Theme
ACCENT = "#72e3fd"
BG = "#0f141a"
SURFACE = "#141b2d"
TEXT = "#e8eef4"
MUTED = "rgba(232,238,244,0.78)"
BORDER = "rgba(114,227,253,0.32)"

APP_DIR = Path(__file__).resolve().parent
USER_DATA = APP_DIR / "user_data"
LOG_DIR = USER_DATA / "logs"
SETTINGS_PATH = USER_DATA / "settings.json"


@dataclass
class Settings:
    # enabled 為 runtime 狀態，不落盤、不讀盤；每次啟動固定 False
    enabled: bool = DEFAULT_ENABLED_RUNTIME

    # SAPI token id；空字串 => 自動
    voice_zh_id: str = ""
    voice_en_id: str = ""

    max_chars: int = DEFAULT_MAX_CHARS
    translate_timeout_sec: float = DEFAULT_TRANSLATE_TIMEOUT_SEC

    font_size: int = DEFAULT_FONT_SIZE
    max_chars_per_line: int = DEFAULT_MAX_CHARS_PER_LINE

    popup_position_mode: str = DEFAULT_POSITION_MODE

    # 全域快捷鍵（pynput 格式，例如 <ctrl>+<alt>+x）；空字串代表不啟用
    global_hotkey: str = ""


class SettingsStore:
    """
    注意：
    - enabled 不寫入、不讀取（避免記住上次啟用狀態）
    - 其他設定會落盤
    - save() 使用 merge 寫入，避免被其他流程洗掉新欄位（例如 font_size）
    """
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Settings:
        with self._lock:
            s = Settings(enabled=DEFAULT_ENABLED_RUNTIME)
            try:
                if not self.path.exists():
                    return s
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k == "enabled":
                            continue
                        if hasattr(s, k):
                            setattr(s, k, v)
                s.enabled = DEFAULT_ENABLED_RUNTIME
                if s.popup_position_mode not in (POS_CURSOR, POS_SCREEN_CENTER):
                    s.popup_position_mode = DEFAULT_POSITION_MODE
                return s
            except Exception as e:
                logging.warning("Settings load failed (using defaults): %s", e)
                return s

    def save(self, s: Settings) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)

            # merge 寫入：避免舊版本/其他流程洗掉新欄位
            existing: Dict[str, Any] = {}
            try:
                if self.path.exists():
                    raw = self.path.read_text(encoding="utf-8")
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        existing = obj
            except Exception as e:
                logging.warning("Settings save (read existing) failed: %s", e)
                existing = {}

            data = asdict(s)
            data.pop("enabled", None)  # enabled 不落盤
            existing.update(data)

            try:
                self.path.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logging.error("Settings save (write) failed: %s", e)


def _ensure_dirs() -> None:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
    )
    logging.info("=== %s 啟動 ===", APP_TITLE)


_RE_ZH = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_RE_EN = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """回傳 'zh' 或 'en'；中文字元 >= 英文字元 => zh，否則 en。"""
    t = (text or "").strip()
    if not t:
        return "en"
    zh = len(_RE_ZH.findall(t))
    en = len(_RE_EN.findall(t))
    if zh == 0 and en == 0:
        return "en"
    return "zh" if zh >= en else "en"


def wrap_by_max_chars(text: str, max_chars_per_line: int) -> str:
    """依「每行最多字數」硬換行（以字元數計），保留原有換行。"""
    if max_chars_per_line <= 0:
        return text or ""
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: List[str] = []
    n = 0
    for ch in s:
        if ch == "\n":
            out.append("\n")
            n = 0
            continue
        out.append(ch)
        n += 1
        if n >= max_chars_per_line:
            out.append("\n")
            n = 0
    return "".join(out).rstrip("\n")


class GoogleTranslator:
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
                translated = "".join((seg[0] or "") for seg in data[0] if isinstance(seg, list) and seg)
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
                token_id = getattr(tok, "Id", "") or ""
                try:
                    desc = tok.GetDescription()
                except Exception:
                    desc = f"Voice {i}"
                try:
                    lang_attr = tok.GetAttribute("Language")
                except Exception:
                    lang_attr = ""
                group = _classify_lcids(_parse_sapi_language_attr(lang_attr))
                infos.append(VoiceInfo(token_id=token_id, description=desc, group=group))
            return infos
        except Exception as e:
            logging.warning("list_voices failed: %s", e)
            return []


class TTSNotifier(QtCore.QObject):
    finished = QtCore.Signal(int)  # token
    stopped = QtCore.Signal(int)   # token


class SapiTTSWorker(threading.Thread):
    """背景 thread：以 SAPI 非同步 Speak，並可 stop。"""
    def __init__(self, notifier: TTSNotifier):
        super().__init__(daemon=True)
        self._q: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._stop_evt = threading.Event()

        self._notifier = notifier
        self._voice = None
        self._token_map: Dict[str, Any] = {}

        self._speaking = False
        self._current_token = 0
        self._speaking_since = 0.0

    def run(self) -> None:
        try:
            if pythoncom is not None:
                pythoncom.CoInitialize()
        except Exception:
            pass

        if win32com is None:
            return

        try:
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
            tokens = self._voice.GetVoices()
            self._token_map = {}
            for i in range(tokens.Count):
                tok = tokens.Item(i)
                tid = getattr(tok, "Id", "") or ""
                if tid:
                    self._token_map[tid] = tok
        except Exception as e:
            logging.error("SAPI init failed: %s", e)
            return

        while not self._stop_evt.is_set():
            try:
                cmd, payload = self._q.get(timeout=0.05)
                if cmd == "QUIT":
                    self._purge()
                    break

                if cmd == "STOP":
                    tok = self._current_token
                    self._purge()
                    self._speaking = False
                    try:
                        self._notifier.stopped.emit(tok)
                    except Exception:
                        pass
                    continue

                if cmd == "SPEAK":
                    text, voice_token_id, tok = payload
                    self._current_token = int(tok)
                    self._speaking_since = time.monotonic()
                    self._speak_async(str(text), str(voice_token_id))
                    self._speaking = True
                    continue

            except queue.Empty:
                pass
            except Exception as e:
                logging.warning("TTS worker queue error: %s", e)

            if self._speaking and self._voice is not None:
                try:
                    st = getattr(self._voice, "Status", None)
                    running = int(getattr(st, "RunningState", 0)) if st is not None else 0
                    if running == 0 and (time.monotonic() - self._speaking_since) > 0.15:
                        tok = self._current_token
                        self._speaking = False
                        try:
                            self._notifier.finished.emit(tok)
                        except Exception:
                            pass
                except Exception:
                    tok = self._current_token
                    self._speaking = False
                    try:
                        self._notifier.finished.emit(tok)
                    except Exception:
                        pass

        try:
            self._purge()
        except Exception:
            pass

        try:
            if pythoncom is not None:
                pythoncom.CoUninitialize()
        except Exception:
            pass

    def _purge(self) -> None:
        # SVSFPurgeBeforeSpeak = 2
        try:
            if self._voice is not None:
                self._voice.Speak("", 2)
        except Exception:
            pass

    def _speak_async(self, text: str, voice_token_id: str) -> None:
        if not text.strip() or self._voice is None:
            return
        self._purge()

        tok = self._token_map.get(voice_token_id) if voice_token_id else None
        if tok is not None:
            try:
                self._voice.Voice = tok
            except Exception:
                pass

        # SVSFlagsAsync = 1
        try:
            self._voice.Speak(text, 1)
        except Exception as e:
            logging.warning("Speak failed: %s", e)

    def speak(self, text: str, voice_token_id: str, token: int) -> None:
        self._q.put(("SPEAK", (text, voice_token_id, int(token))))

    def stop(self) -> None:
        self._q.put(("STOP", None))

    def quit(self) -> None:
        self._q.put(("QUIT", None))
        self._stop_evt.set()


# -----------------------------
# 全域滑鼠監聽（關閉彈窗/滾輪關閉）
# -----------------------------
class GlobalMouseWatcher(QtCore.QObject):
    clicked = QtCore.Signal(int, int)  # x, y
    wheel = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._listener = None

    def start(self) -> None:
        if mouse is None:
            logging.info("pynput.mouse 不可用：無法全域關閉彈窗（點擊/滾輪）")
            return

        def on_click(x, y, button, pressed):
            if pressed:
                try:
                    self.clicked.emit(int(x), int(y))
                except Exception:
                    pass

        def on_scroll(x, y, dx, dy):
            try:
                self.wheel.emit()
            except Exception:
                pass

        try:
            self._listener = mouse.Listener(on_click=on_click, on_scroll=on_scroll)
            self._listener.daemon = True
            self._listener.start()
            logging.info("GlobalMouseWatcher started")
        except Exception as e:
            logging.warning("GlobalMouseWatcher failed: %s", e)

    def stop(self) -> None:
        try:
            if self._listener:
                self._listener.stop()
        except Exception:
            pass


# -----------------------------
# 全域快捷鍵監聽
# -----------------------------
class GlobalHotkeyWatcher(QtCore.QObject):
    triggered = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self._listener = None
        self._hotkey_str = ""

    def update_hotkey(self, hotkey_str: str) -> None:
        new_str = (hotkey_str or "").strip()
        if new_str == self._hotkey_str:
            return

        self.stop()
        self._hotkey_str = new_str

        if not self._hotkey_str:
            return

        if keyboard is None:
            logging.warning("pynput.keyboard 不可用：無法啟用全域快捷鍵")
            return

        try:
            self._listener = keyboard.GlobalHotKeys({self._hotkey_str: self._on_activate})
            self._listener.start()
            logging.info("GlobalHotkeyWatcher started: %s", self._hotkey_str)
        except Exception as e:
            logging.warning("Failed to start hotkey listener (%s): %s", self._hotkey_str, e)
            self._listener = None

    def _on_activate(self) -> None:
        self.triggered.emit()

    def stop(self) -> None:
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


# -----------------------------
# UI：Popup（只顯示中文翻譯 + 播放/停止）
# -----------------------------
class PopupWidget(QtWidgets.QWidget):
    play_toggle = QtCore.Signal()
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

        self._text_src = ""
        self._text_zh = ""
        self._playing = False

        self._font_size = DEFAULT_FONT_SIZE
        self._max_chars_per_line = DEFAULT_MAX_CHARS_PER_LINE

        self._build_ui()

    def _build_ui(self) -> None:
        root = QtWidgets.QFrame()
        root.setObjectName("popupRoot")

        self.btn_play = QtWidgets.QToolButton()
        self.btn_play.setObjectName("btnPlay")
        self.btn_play.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_play.setIconSize(QtCore.QSize(26, 26))
        self.btn_play.setFixedSize(64, 64)
        self.btn_play.clicked.connect(self.play_toggle.emit)

        self.lbl_zh = QtWidgets.QLabel()
        self.lbl_zh.setObjectName("lblZh")
        self.lbl_zh.setWordWrap(True)
        self.lbl_zh.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        lay = QtWidgets.QHBoxLayout(root)
        lay.setContentsMargins(16, 16, 18, 16)
        lay.setSpacing(14)
        lay.addWidget(self.btn_play, 0, QtCore.Qt.AlignVCenter)
        lay.addWidget(self.lbl_zh, 1)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.addWidget(root)

        # 移除陰影
        # shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        # shadow.setBlurRadius(26)
        # shadow.setOffset(0, 10)
        # shadow.setColor(QtGui.QColor(0, 0, 0, 130))
        # root.setGraphicsEffect(shadow)

        self.setStyleSheet(self._style_sheet())
        self.set_playing(False)
        self.set_enabled_play(False)

        self.apply_display_settings(self._font_size, self._max_chars_per_line)

    def _style_sheet(self, btn_radius: int = 32) -> str:
        return f"""
        #popupRoot {{
            background: rgba(20, 27, 45, 0.96);
            border: 1px solid {BORDER};
            border-radius: 20px;
        }}
        #btnPlay {{
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: {btn_radius}px;
        }}
        #btnPlay:hover {{
            border-color: {ACCENT};
            background: rgba(114,227,253,0.12);
        }}
        #btnPlay:pressed {{
            background: rgba(114,227,253,0.18);
        }}
        """

    def _icon_play(self) -> QtGui.QIcon:
        sz = self.btn_play.iconSize().width()
        if sz < 10:
            sz = 26
        pm = QtGui.QPixmap(sz, sz)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(114, 227, 253, 255))
        w, h = float(sz), float(sz)
        points = [
            QtCore.QPointF(w * 0.375, h * 0.29),
            QtCore.QPointF(w * 0.375, h * 0.71),
            QtCore.QPointF(w * 0.71, h * 0.5),
        ]
        p.drawPolygon(QtGui.QPolygonF(points))
        p.end()
        return QtGui.QIcon(pm)

    def _icon_stop(self) -> QtGui.QIcon:
        sz = self.btn_play.iconSize().width()
        if sz < 10:
            sz = 26
        pm = QtGui.QPixmap(sz, sz)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(114, 227, 253, 255))
        w, h = float(sz), float(sz)
        p.drawRoundedRect(QtCore.QRectF(w * 0.333, h * 0.333, w * 0.333, h * 0.333), w * 0.08, h * 0.08)
        p.end()
        return QtGui.QIcon(pm)

    def apply_display_settings(self, font_size: int, max_chars_per_line: int) -> None:
        self._font_size = max(10, int(font_size))
        self._max_chars_per_line = max(1, int(max_chars_per_line))

        # 讓播放按鈕隨字體大小縮放
        btn_dim = int(self._font_size * 2.8)
        icon_dim = int(btn_dim * 0.4)
        radius = btn_dim // 2

        self.btn_play.setFixedSize(btn_dim, btn_dim)
        self.btn_play.setIconSize(QtCore.QSize(icon_dim, icon_dim))
        self.setStyleSheet(self._style_sheet(btn_radius=radius))

        # 重新產生圖示（因為大小變了）
        self.set_playing(self._playing)

        # 兩道保險：pixelSize + QLabel 自身 QSS font-size(px)
        f = self.lbl_zh.font()
        f.setPixelSize(self._font_size)
        self.lbl_zh.setFont(f)
        self.lbl_zh.setStyleSheet(
            f"color: {TEXT}; font-size: {self._font_size}px; font-weight: 600;"
        )

        self._re_render_text()

    def _re_render_text(self) -> None:
        if self._text_zh:
            shown = wrap_by_max_chars(self._text_zh, self._max_chars_per_line)
            self.lbl_zh.setText(shown)
        else:
            self.lbl_zh.setText("翻譯中…")
        self.adjustSize()

    def set_enabled_play(self, ok: bool) -> None:
        self.btn_play.setEnabled(bool(ok))
        self.btn_play.setToolTip("播放/停止" if ok else "TTS 不可用")

    def set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        self.btn_play.setIcon(self._icon_stop() if self._playing else self._icon_play())

    def show_popup(self, src_text: str, zh_text: str, position_mode: str) -> None:
        self._text_src = src_text
        self._text_zh = zh_text
        self._re_render_text()
        self._move_by_mode(position_mode)
        self.show()

    def _move_by_mode(self, position_mode: str) -> None:
        pos = QtGui.QCursor.pos()
        cx, cy = int(pos.x()), int(pos.y())

        screen = QtGui.QGuiApplication.screenAt(pos) or QtGui.QGuiApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1920, 1080)

        if position_mode == POS_SCREEN_CENTER:
            x = geo.center().x() - self.width() // 2
            y = geo.center().y() - self.height() // 2
        else:
            x = cx + 18
            y = cy + 18

        if x + self.width() > geo.right():
            x = max(geo.left(), geo.right() - self.width())
        if y + self.height() > geo.bottom():
            y = max(geo.top(), geo.bottom() - self.height())
        if x < geo.left():
            x = geo.left()
        if y < geo.top():
            y = geo.top()

        self.move(int(x), int(y))

    def update_zh_text(self, zh_text: str) -> None:
        self._text_zh = zh_text
        self._re_render_text()

    def hide_and_emit(self) -> None:
        self.hide()
        self.closed.emit()

    def contains_global_point(self, x: int, y: int) -> bool:
        if not self.isVisible():
            return False
        top_left = self.mapToGlobal(QtCore.QPoint(0, 0))
        rect = QtCore.QRect(top_left, self.size())
        return rect.contains(QtCore.QPoint(int(x), int(y)))

    @property
    def src_text(self) -> str:
        return self._text_src


# -----------------------------
# UI：設定視窗
# -----------------------------
class SettingsDialog(QtWidgets.QDialog):
    settings_changed = QtCore.Signal(Settings)
    request_quit = QtCore.Signal()

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        store: SettingsStore,
        sapi_voices: List[VoiceInfo],
        tts_available: bool,
        runtime_enabled: bool,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_TITLE}｜設定")
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setMinimumWidth(600)

        self._store = store
        self._voices = sapi_voices
        self._tts_available = tts_available

        self._s = store.load()
        self._s.enabled = bool(runtime_enabled)

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
            QCheckBox {{ spacing: 8px; }}
            QComboBox, QSpinBox, QDoubleSpinBox {{
                background: {SURFACE};
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 10px;
                padding: 6px 10px;
            }}
            QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
                border-color: {ACCENT};
            }}
            QPushButton {{
                background: rgba(114,227,253,0.12);
                border: 1px solid rgba(114,227,253,0.35);
                border-radius: 10px;
                padding: 8px 12px;
            }}
            QPushButton:hover {{
                background: rgba(114,227,253,0.18);
                border-color: {ACCENT};
            }}
            QPushButton:pressed {{
                background: rgba(114,227,253,0.22);
            }}
            """
        )

    def _build_ui(self) -> None:
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(12)

        self.chk_enabled = QtWidgets.QCheckBox("啟用（剪貼簿文字變更時顯示彈窗）")
        lay.addWidget(self.chk_enabled)

        grp_voice = QtWidgets.QGroupBox("語音（朗讀原文：自動判斷中文/英文）")
        vlay = QtWidgets.QFormLayout(grp_voice)
        vlay.setContentsMargins(12, 12, 12, 12)
        vlay.setSpacing(10)

        self.cmb_zh = QtWidgets.QComboBox()
        self.cmb_en = QtWidgets.QComboBox()
        self.lbl_tts_state = QtWidgets.QLabel("")
        self.lbl_tts_state.setStyleSheet(f"color: {MUTED};")

        vlay.addRow("中文語音（zh-*）：", self.cmb_zh)
        vlay.addRow("英文語音（en-*）：", self.cmb_en)
        vlay.addRow("TTS 狀態：", self.lbl_tts_state)
        lay.addWidget(grp_voice)

        grp_display = QtWidgets.QGroupBox("顯示")
        dlay = QtWidgets.QFormLayout(grp_display)
        dlay.setContentsMargins(12, 12, 12, 12)
        dlay.setSpacing(10)

        self.spn_font = QtWidgets.QSpinBox()
        self.spn_font.setRange(12, 72)
        self.spn_font.setSingleStep(1)

        self.spn_line = QtWidgets.QSpinBox()
        self.spn_line.setRange(6, 80)
        self.spn_line.setSingleStep(1)

        self.cmb_pos = QtWidgets.QComboBox()
        self.cmb_pos.addItem("滑鼠旁邊", userData=POS_CURSOR)
        self.cmb_pos.addItem("顯示器正中心", userData=POS_SCREEN_CENTER)

        self.lbl_pos_hint = QtWidgets.QLabel("")
        self.lbl_pos_hint.setStyleSheet(f"color: {MUTED};")

        dlay.addRow("字體大小：", self.spn_font)
        dlay.addRow("每行最多字數：", self.spn_line)
        dlay.addRow("彈窗位置：", self.cmb_pos)
        dlay.addRow("提示：", self.lbl_pos_hint)
        lay.addWidget(grp_display)

        grp_misc = QtWidgets.QGroupBox("翻譯與操作")
        mlay = QtWidgets.QFormLayout(grp_misc)
        mlay.setContentsMargins(12, 12, 12, 12)
        mlay.setSpacing(10)

        self.dsp_timeout = QtWidgets.QDoubleSpinBox()
        self.dsp_timeout.setRange(1.0, 30.0)
        self.dsp_timeout.setSingleStep(0.5)
        self.dsp_timeout.setSuffix(" 秒")

        self.spn_max = QtWidgets.QSpinBox()
        self.spn_max.setRange(50, 5000)
        self.spn_max.setSingleStep(50)

        self.chk_hk_ctrl = QtWidgets.QCheckBox("Ctrl")
        self.chk_hk_alt = QtWidgets.QCheckBox("Alt")
        self.chk_hk_shift = QtWidgets.QCheckBox("Shift")
        self.cmb_hk_key = QtWidgets.QComboBox()
        self.cmb_hk_key.addItem("（無）", userData="")
        for i in range(10):
            self.cmb_hk_key.addItem(str(i), userData=str(i))
        for i in range(ord('a'), ord('z') + 1):
            c = chr(i)
            self.cmb_hk_key.addItem(c.upper(), userData=c)

        hk_widget = QtWidgets.QWidget()
        hk_lay = QtWidgets.QHBoxLayout(hk_widget)
        hk_lay.setContentsMargins(0, 0, 0, 0)
        hk_lay.addWidget(self.chk_hk_ctrl)
        hk_lay.addWidget(self.chk_hk_alt)
        hk_lay.addWidget(self.chk_hk_shift)
        hk_lay.addWidget(self.cmb_hk_key, 1)

        mlay.addRow("翻譯逾時：", self.dsp_timeout)
        mlay.addRow("最長字數：", self.spn_max)
        mlay.addRow("全域快捷鍵：", hk_widget)
        lay.addWidget(grp_misc)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)

        self.btn_save = QtWidgets.QPushButton("儲存")
        self.btn_save.clicked.connect(self._on_save)

        self.btn_close = QtWidgets.QPushButton("關閉")
        self.btn_close.clicked.connect(self.close)

        self.btn_quit = QtWidgets.QPushButton("結束程式")
        self.btn_quit.clicked.connect(self.request_quit.emit)

        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_close)
        btn_row.addWidget(self.btn_quit)
        lay.addLayout(btn_row)

        self._populate_voices()

        self.lbl_tts_state.setText("可用" if self._tts_available else "不可用（未安裝 pywin32 或 SAPI 初始化失敗）")
        self.lbl_pos_hint.setText("「滑鼠旁邊」更像翻譯泡泡；「顯示器正中心」適合固定閱讀。")

    def _populate_voices(self) -> None:
        self.cmb_zh.clear()
        self.cmb_en.clear()

        if not self._voices:
            self.cmb_zh.addItem("（未偵測到語音；請安裝 pywin32 / Windows 語音）", userData="")
            self.cmb_en.addItem("（未偵測到語音；請安裝 pywin32 / Windows 語音）", userData="")
            return

        zh = [v for v in self._voices if v.group == "zh"] or self._voices[:]
        en = [v for v in self._voices if v.group == "en"] or self._voices[:]

        self.cmb_zh.addItem("（自動選擇）", userData="")
        for v in zh:
            self.cmb_zh.addItem(v.description, userData=v.token_id)

        self.cmb_en.addItem("（自動選擇）", userData="")
        for v in en:
            self.cmb_en.addItem(v.description, userData=v.token_id)

    def _load_to_ui(self) -> None:
        s = self._s
        self.chk_enabled.setChecked(bool(s.enabled))
        self.dsp_timeout.setValue(float(s.translate_timeout_sec))
        self.spn_max.setValue(int(s.max_chars))
        self.spn_font.setValue(int(s.font_size))
        self.spn_line.setValue(int(s.max_chars_per_line))

        # Parse hotkey string (e.g. <ctrl>+<alt>+x)
        hotkey = (s.global_hotkey or "").lower()
        parts = [p.strip() for p in hotkey.split("+") if p.strip()]
        self.chk_hk_ctrl.setChecked("<ctrl>" in parts)
        self.chk_hk_alt.setChecked("<alt>" in parts)
        self.chk_hk_shift.setChecked("<shift>" in parts)

        key_val = ""
        for p in parts:
            if not (p.startswith("<") and p.endswith(">")):
                key_val = p
                break
        idx = self.cmb_hk_key.findData(key_val)
        self.cmb_hk_key.setCurrentIndex(idx if idx >= 0 else 0)

        idx = self.cmb_zh.findData(s.voice_zh_id)
        self.cmb_zh.setCurrentIndex(idx if idx >= 0 else 0)

        idx = self.cmb_en.findData(s.voice_en_id)
        self.cmb_en.setCurrentIndex(idx if idx >= 0 else 0)

        idx = self.cmb_pos.findData(s.popup_position_mode)
        self.cmb_pos.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_save(self) -> None:
        parts = []
        if self.chk_hk_ctrl.isChecked():
            parts.append("<ctrl>")
        if self.chk_hk_alt.isChecked():
            parts.append("<alt>")
        if self.chk_hk_shift.isChecked():
            parts.append("<shift>")
        k = self.cmb_hk_key.currentData()
        if k:
            parts.append(str(k))
        final_hotkey = "+".join(parts) if k else ""

        s = Settings(
            enabled=self.chk_enabled.isChecked(),  # runtime only（不落盤）
            voice_zh_id=str(self.cmb_zh.currentData() or ""),
            voice_en_id=str(self.cmb_en.currentData() or ""),
            max_chars=int(self.spn_max.value()),
            translate_timeout_sec=float(self.dsp_timeout.value()),
            font_size=int(self.spn_font.value()),
            max_chars_per_line=int(self.spn_line.value()),
            popup_position_mode=str(self.cmb_pos.currentData() or DEFAULT_POSITION_MODE),
            global_hotkey=final_hotkey,
        )
        self._store.save(s)
        self._s = s
        self.settings_changed.emit(s)


# -----------------------------
# 主程式控制器
# -----------------------------
class AppController(QtCore.QObject):
    translation_ready = QtCore.Signal(str, str)  # (src_text, zh_text)

    def __init__(self, app: QtWidgets.QApplication):
        super().__init__()
        self.app = app
        self.store = SettingsStore(SETTINGS_PATH)

        self.settings = self.store.load()
        self.runtime_enabled = DEFAULT_ENABLED_RUNTIME
        self.settings.enabled = self.runtime_enabled

        self.translator = GoogleTranslator()

        self.popup = PopupWidget()
        self.popup.play_toggle.connect(self.on_play_toggle)
        self.popup.closed.connect(self.on_popup_closed)

        self.mouse_watcher = GlobalMouseWatcher()
        self.mouse_watcher.clicked.connect(self.on_global_click)
        self.mouse_watcher.wheel.connect(self.on_global_wheel)
        self.mouse_watcher.start()

        self.hotkey_watcher = GlobalHotkeyWatcher()
        self.hotkey_watcher.triggered.connect(self.on_hotkey_triggered)
        self.hotkey_watcher.update_hotkey(self.settings.global_hotkey)

        self.clipboard = self.app.clipboard()
        self.clipboard.dataChanged.connect(self.on_clipboard_changed)

        self.tts_notifier = TTSNotifier()
        self.tts_notifier.finished.connect(self.on_tts_finished)
        self.tts_notifier.stopped.connect(self.on_tts_stopped)

        self._tts_worker: Optional[SapiTTSWorker] = None
        self._tts_available = False
        if win32com is not None:
            try:
                self._tts_worker = SapiTTSWorker(self.tts_notifier)
                self._tts_worker.start()
                self._tts_available = True
            except Exception as e:
                logging.warning("TTS worker start failed: %s", e)
                self._tts_worker = None
                self._tts_available = False

        self._play_token = 0
        self.translation_ready.connect(self.on_translation_ready)

        self._settings_dialog: Optional[SettingsDialog] = None
        self._tray = self._create_tray()

        self.apply_popup_settings()

        self._tray.showMessage(
            APP_TITLE,
            "已啟動（本次執行預設未啟用；不記住上次狀態）。右鍵系統匣圖示 → 勾選「啟用」。",
            QtWidgets.QSystemTrayIcon.Information,
            4500,
        )

        if mouse is None:
            self._tray.showMessage(
                APP_TITLE,
                "提示：未安裝 pynput，無法做到「點擊/滾輪在任何地方關閉彈窗」。",
                QtWidgets.QSystemTrayIcon.Warning,
                4500,
            )
        if not self._tts_available:
            self._tray.showMessage(
                APP_TITLE,
                "提示：未安裝 pywin32 或 SAPI 不可用，播放功能將無法使用。",
                QtWidgets.QSystemTrayIcon.Warning,
                4500,
            )

    def apply_popup_settings(self, s: Optional[Settings] = None) -> None:
        if s is None:
            self.settings = self.store.load()
        else:
            self.settings = s
        self.popup.apply_display_settings(self.settings.font_size, self.settings.max_chars_per_line)
        self.hotkey_watcher.update_hotkey(self.settings.global_hotkey)

    def _create_tray(self) -> QtWidgets.QSystemTrayIcon:
        tray = QtWidgets.QSystemTrayIcon(self._make_tray_icon(), self.app)
        tray.setToolTip(APP_TITLE)

        menu = QtWidgets.QMenu()

        self.act_enabled = QtGui.QAction("啟用", menu)
        self.act_enabled.setCheckable(True)
        self.act_enabled.setChecked(False)
        self.act_enabled.toggled.connect(self.set_enabled)

        act_settings = QtGui.QAction("設定…", menu)
        act_settings.triggered.connect(self.open_settings)

        act_test = QtGui.QAction("測試彈窗", menu)
        act_test.triggered.connect(self.test_popup)

        act_quit = QtGui.QAction("結束", menu)
        act_quit.triggered.connect(self.quit)

        menu.addAction(self.act_enabled)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addAction(act_test)
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
        p.setBrush(QtGui.QColor(20, 27, 45, 255))
        p.drawEllipse(4, 4, size - 8, size - 8)
        pen = QtGui.QPen(QtGui.QColor(114, 227, 253, 255))
        pen.setWidth(4)
        p.setPen(pen)
        p.setBrush(QtGui.QColor(114, 227, 253, 70))
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
            dlg = SettingsDialog(
                None,
                self.store,
                voices,
                tts_available=self._tts_available,
                runtime_enabled=self.runtime_enabled,
            )
            dlg.settings_changed.connect(self.on_settings_changed)
            dlg.request_quit.connect(self.quit)
            self._settings_dialog = dlg
        else:
            self._settings_dialog._voices = voices  # noqa: SLF001
            self._settings_dialog._populate_voices()  # noqa: SLF001
            s = self.store.load()
            s.enabled = self.runtime_enabled
            self._settings_dialog._s = s  # noqa: SLF001
            self._settings_dialog._load_to_ui()  # noqa: SLF001

        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def on_settings_changed(self, s: Settings) -> None:
        # enabled 是 runtime
        self.set_enabled(bool(s.enabled))
        # 其餘設定：直接使用傳入的 s，不需再讀一次硬碟
        self.apply_popup_settings(s)
        logging.info(
            "settings updated: runtime_enabled=%s font_size=%s max_chars_per_line=%s pos=%s",
            self.runtime_enabled,
            self.settings.font_size,
            self.settings.max_chars_per_line,
            self.settings.popup_position_mode,
        )

    def set_enabled(self, enabled: bool) -> None:
        # 注意：不要在這裡 store.save()，避免用舊物件覆寫 settings.json
        self.runtime_enabled = bool(enabled)
        self.act_enabled.blockSignals(True)
        self.act_enabled.setChecked(self.runtime_enabled)
        self.act_enabled.blockSignals(False)
        self._tray.showMessage(
            APP_TITLE,
            f"已{'啟用' if enabled else '停用'}（本次執行，不會記住）",
            QtWidgets.QSystemTrayIcon.Information,
            1500,
        )
        logging.info("runtime enabled set to %s (not persisted)", enabled)

    def test_popup(self) -> None:
        self.show_popup("There are many traffic lights on the street.")

    @QtCore.Slot()
    def on_hotkey_triggered(self) -> None:
        # 快捷鍵觸發：若未啟用則不動作
        if not self.runtime_enabled:
            return
        text = (self.clipboard.text() or "").strip()
        if text:
            self.show_popup(text)

    # -------------------------
    # Clipboard trigger
    # -------------------------
    def on_clipboard_changed(self) -> None:
        if not self.runtime_enabled:
            return
        text = (self.clipboard.text() or "").strip()
        if not text:
            return
        if len(text) > int(self.settings.max_chars):
            return
        QtCore.QTimer.singleShot(60, lambda: self._show_if_still_same(text))

    def _show_if_still_same(self, expected: str) -> None:
        cur = (self.clipboard.text() or "").strip()
        if cur != expected:
            return
        self.show_popup(cur)

    # -------------------------
    # Popup show + translate
    # -------------------------
    def show_popup(self, src_text: str) -> None:
        self._stop_tts()
        self.popup.set_playing(False)

        self.popup.set_enabled_play(self._tts_available)
        # 修正：使用當前記憶體中的設定，不要重新讀取硬碟（避免存檔延遲導致跳回舊值）
        self.apply_popup_settings(self.settings)

        # 先顯示（中文尚未準備好）
        self.popup.show_popup(
            src_text=src_text,
            zh_text="",
            position_mode=self.settings.popup_position_mode,
        )

        src_lang = detect_lang(src_text)
        if src_lang == "zh":
            self.translation_ready.emit(src_text, src_text)
            return

        th = threading.Thread(
            target=self._translate_worker,
            args=(src_text, float(self.settings.translate_timeout_sec)),
            daemon=True,
        )
        th.start()

    def _translate_worker(self, src_text: str, timeout: float) -> None:
        zh = self.translator.translate(src_text, sl="en", tl="zh-TW", timeout_sec=timeout)
        self.translation_ready.emit(src_text, zh)

    @QtCore.Slot(str, str)
    def on_translation_ready(self, src_text: str, zh_text: str) -> None:
        if not self.popup.isVisible() or self.popup.src_text != src_text:
            return
        self.popup.update_zh_text(zh_text if zh_text else "（翻譯失敗或被阻擋）")

    # -------------------------
    # Play/Stop toggle（朗讀原文）
    # -------------------------
    def on_play_toggle(self) -> None:
        if not self.popup.isVisible():
            return
        if not self._tts_available or self._tts_worker is None:
            return

        if self.popup._playing:
            self._stop_tts()
            self.popup.set_playing(False)
            return

        src = (self.popup.src_text or "").strip()
        if not src:
            return

        lang = detect_lang(src)
        voice_id = self.settings.voice_zh_id if lang == "zh" else self.settings.voice_en_id

        self._play_token += 1
        tok = self._play_token
        self.popup.set_playing(True)
        self._tts_worker.speak(src, voice_id, token=tok)

    def _stop_tts(self) -> None:
        try:
            if self._tts_worker is not None:
                self._tts_worker.stop()
        except Exception:
            pass

    @QtCore.Slot(int)
    def on_tts_finished(self, token: int) -> None:
        if token == self._play_token:
            self.popup.set_playing(False)

    @QtCore.Slot(int)
    def on_tts_stopped(self, token: int) -> None:
        if token == self._play_token:
            self.popup.set_playing(False)

    def on_popup_closed(self) -> None:
        self._stop_tts()
        self.popup.set_playing(False)

    # -------------------------
    # Global close behavior
    # -------------------------
    @QtCore.Slot(int, int)
    def on_global_click(self, x: int, y: int) -> None:
        if self.popup.isVisible() and (not self.popup.contains_global_point(x, y)):
            self.popup.hide_and_emit()

    @QtCore.Slot()
    def on_global_wheel(self) -> None:
        if self.popup.isVisible():
            self.popup.hide_and_emit()

    # -------------------------
    # Quit
    # -------------------------
    def quit(self) -> None:
        try:
            self.popup.hide()
        except Exception:
            pass
        try:
            self._stop_tts()
        except Exception:
            pass
        try:
            if self._tts_worker is not None:
                self._tts_worker.quit()
        except Exception:
            pass
        try:
            self.mouse_watcher.stop()
        except Exception:
            pass
        try:
            self.hotkey_watcher.stop()
        except Exception:
            pass
        try:
            self._tray.hide()
        except Exception:
            pass
        logging.info("=== %s 結束 ===", APP_TITLE)
        self.app.quit()


def main() -> int:
    _ensure_dirs()
    setup_logging()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)

    _ = AppController(app)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
