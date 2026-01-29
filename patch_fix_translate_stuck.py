# -*- coding: utf-8 -*-
"""
patch_fix_translate_stuck.py

修正 bug：彈窗一直顯示「翻譯中…」不更新

原因：
- 原本程式在「背景 thread」完成翻譯後，用 QTimer.singleShot(0, apply) 企圖回到 GUI thread 更新 UI。
- 但 Qt/PySide 的 QTimer 需要「啟動 timer 的 thread」有 event loop；一般 Python Thread 沒有 Qt event loop，
  singleShot 可能永遠不觸發，導致 UI 永遠停在「翻譯中…」。

修正：
- 改用 Qt Signal（Queued connection）把翻譯結果從背景 thread 傳回 GUI thread 更新 UI。

使用：
  1) 把本檔案放在與 app__.py 同一層
  2) 在該資料夾執行：python patch_fix_translate_stuck.py
  3) 會建立備份：app__.py.bak.YYYYMMDD-HHMMSS
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

MARKER = "# [PATCH] fix-translate-stuck: use-signal\n"


def backup_file(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{ts}")
    bak.write_bytes(path.read_bytes())
    return bak


def ensure_signal_decl(src: str) -> tuple[str, int]:
    if MARKER in src:
        return src, 0

    # 在 class AppController(QtCore.QObject): 後插入 translation_ready Signal
    pat = re.compile(r"^(class\s+AppController\s*\(\s*QtCore\.QObject\s*\)\s*:\s*\r?\n)", re.M)
    m = pat.search(src)
    if not m:
        return src, 0

    insert = (
        m.group(1)
        + "    " + MARKER
        + "    translation_ready = QtCore.Signal(str, str)\n"
    )
    out = src[:m.start(1)] + insert + src[m.end(1):]
    return out, 1


def ensure_connect_in_init(src: str) -> tuple[str, int]:
    # 在 __init__ 設定 popup signal 後插入 connect
    if "self.translation_ready.connect(self.on_translation_ready)" in src:
        return src, 0

    # 找一個穩定錨點：popup translate_clicked connect 的那行後面
    pat = re.compile(r"^(?P<indent>[ \t]*)self\.popup\.translate_clicked\.connect\(self\.on_translate_clicked\)\s*\r?\n", re.M)
    m = pat.search(src)
    if not m:
        return src, 0

    indent = m.group("indent")
    insert = m.group(0) + f"{indent}self.translation_ready.connect(self.on_translation_ready)\n"
    out = src[:m.start()] + insert + src[m.end():]
    return out, 1


def replace_translate_update(src: str) -> tuple[str, int]:
    """
    把 _translate_and_update 的 QTimer.singleShot 方式改成 emit signal，
    並補上 on_translation_ready slot。
    """
    if "def on_translation_ready" in src and "self.translation_ready.emit" in src:
        return src, 0

    # 擷取 _translate_and_update 整段（直到 QtCore.QTimer.singleShot(0, apply) 那行）
    pat = re.compile(
        r"""
        ^(?P<indent>[ \t]*)def\s+_translate_and_update\s*\(.*?\)\s*->\s*None\s*:\s*\r?\n
        (?P<body>.*?)
        ^(?P=indent)[ \t]*QtCore\.QTimer\.singleShot\(\s*0\s*,\s*apply\s*\)\s*\r?\n
        """,
        re.M | re.S | re.X
    )
    m = pat.search(src)
    if not m:
        return src, 0

    indent = m.group("indent")
    # 重新定義這個方法 + slot
    replacement = (
        f"{indent}def _translate_and_update(self, text: str, sl: str, tl: str, timeout: float) -> None:\n"
        f"{indent}    translated = self.translator.translate(text, sl=sl, tl=tl, timeout_sec=timeout)\n"
        f"{indent}    # 使用 Qt signal（跨 thread queued）回到 GUI thread 更新 UI\n"
        f"{indent}    self.translation_ready.emit(text, translated)\n"
        f"\n"
        f"{indent}@QtCore.Slot(str, str)\n"
        f"{indent}def on_translation_ready(self, text: str, translated: str) -> None:\n"
        f"{indent}    if self.popup.isVisible() and self.popup.text == text:\n"
        f"{indent}        self.popup.update_translation(translated)\n"
        f"\n"
    )

    out = src[:m.start()] + replacement + src[m.end():]
    return out, 1


def main() -> int:
    here = Path(__file__).resolve().parent
    target = here / "app__.py"
    if not target.exists():
        print(f"[ERROR] 找不到 app__.py：{target}")
        print("請把本 patch 檔放到與 app__.py 同一層再執行。")
        return 2

    src = target.read_text(encoding="utf-8", errors="replace")

    src1, n1 = ensure_signal_decl(src)
    src2, n2 = ensure_connect_in_init(src1)
    src3, n3 = replace_translate_update(src2)

    total = n1 + n2 + n3
    if total == 0:
        if MARKER in src:
            print("[OK] 已套用過 patch（不需重複）。")
            return 0
        print("[WARN] 沒找到可套用的片段，可能 app__.py 版本不同或你已手動改過。")
        print("請確認 app__.py 內是否存在：_translate_and_update 與 QtCore.QTimer.singleShot(0, apply)")
        return 1

    bak = backup_file(target)
    target.write_text(src3, encoding="utf-8")
    print("[OK] 已套用 patch（修正翻譯卡在「翻譯中…」）。")
    print(f" - 備份：{bak.name}")
    print(" - 變更：背景 thread 完成翻譯後改用 Qt signal 回到 GUI thread 更新彈窗")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
