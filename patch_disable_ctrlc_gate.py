# -*- coding: utf-8 -*-
"""
patch_disable_ctrlc_gate.py

用途：在「同一個資料夾」執行後，直接修改 app__.py，讓觸發不再依賴 Ctrl+C 全域監聽成功與否。
作法：移除/註解 on_clipboard_changed() 內的「Ctrl+C 時窗」判斷：
    if (time.monotonic() - self._last_ctrl_c_ts) > 0.8:
        return

使用：
  1) 把本檔案放在與 app__.py 相同的資料夾
  2) 在該資料夾開終端機執行：python patch_disable_ctrlc_gate.py
  3) 會自動建立備份：app__.py.bak.YYYYMMDD-HHMMSS

注意：此 patch 不會移除 pynput/HotkeyWatcher，只是不再把它當成觸發必須條件。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path


MARKER = "# [PATCH] disable-ctrlc-gate: clipboard-only trigger\n"


def backup_file(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{ts}")
    bak.write_bytes(path.read_bytes())
    return bak


def patch_text(src: str) -> tuple[str, int]:
    """
    回傳：(patched_text, replacements_count)
    以多組 pattern 盡量涵蓋不同排版。
    """
    if MARKER in src:
        return src, 0

    # 目標：刪除/註解這段 gate（中間可能有註解/空白）
    patterns = [
        # 最常見：兩行
        r"""
        ^(?P<indent>[ \t]*)
        if[ \t]*\([ \t]*time\.monotonic\(\)[ \t]*-[ \t]*self\._last_ctrl_c_ts[ \t]*\)[ \t]*>[ \t]*0\.8[ \t]*:[ \t]*\r?\n
        (?P=indent)[ \t]*return[ \t]*\r?\n
        """,
        # 有時會用 <= 或其他寫法；先以 0.8 為錨點
        r"""
        ^(?P<indent>[ \t]*)
        if[ \t]*\([^\n]*self\._last_ctrl_c_ts[^\n]*0\.8[^\n]*\)[ \t]*:[ \t]*\r?\n
        (?P=indent)[ \t]*return[ \t]*\r?\n
        """,
    ]

    patched = src
    total = 0
    for pat in patterns:
        rgx = re.compile(pat, re.VERBOSE | re.MULTILINE)
        def repl(m: re.Match) -> str:
            indent = m.group("indent")
            return (
                f"{indent}{MARKER}"
                f"{indent}# 原本的 Ctrl+C 時窗 gate 已停用，改為只要剪貼簿文字變更就觸發。\n"
            )
        patched, n = rgx.subn(repl, patched, count=1)
        total += n
        if n:
            break

    return patched, total


def main() -> int:
    here = Path(__file__).resolve().parent
    target = here / "app__.py"
    if not target.exists():
        print(f"[ERROR] 找不到目標檔：{target}")
        print("請把本 patch 檔放到與 app__.py 同一層再執行。")
        return 2

    src = target.read_text(encoding="utf-8", errors="replace")
    patched, n = patch_text(src)

    if n == 0:
        if MARKER in src:
            print("[OK] app__.py 已經套用過 patch（不需重複）。")
            return 0
        print("[WARN] 沒找到可替換的 Ctrl+C gate 段落。")
        print("可能原因：你已手動修改過，或 app__.py 版本不同。")
        print("你可以用搜尋找：self._last_ctrl_c_ts 或 time.monotonic() - self._last_ctrl_c_ts")
        return 1

    bak = backup_file(target)
    target.write_text(patched, encoding="utf-8")
    print("[OK] 已套用 patch。")
    print(f" - 備份：{bak.name}")
    print(" - 變更：停用 Ctrl+C 時窗 gate（剪貼簿文字變更即可觸發）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
