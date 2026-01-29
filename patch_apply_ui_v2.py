# -*- coding: utf-8 -*-
"""
patch_apply_ui_v2.py

用途：把「彈窗只顯示中文翻譯 + 播放/停止 + 點擊外部/滾輪即關閉」的新版 app__.py 套用到目前資料夾。
- 會先備份：app__.py.bak.YYYYMMDD-HHMMSS
- 再把 app__.py 替換成 app__.py.ui_v2 的內容（同資料夾需存在 app__.py.ui_v2）

使用：
  1) 確保本檔與 app__.py.ui_v2 都在與 app__.py 同一層
  2) python patch_apply_ui_v2.py
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def backup_file(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{ts}")
    bak.write_bytes(path.read_bytes())
    return bak


def main() -> int:
    here = Path(__file__).resolve().parent
    src = here / "app__.py.ui_v2"
    dst = here / "app__.py"

    if not src.exists():
        print(f"[ERROR] 找不到 {src.name}（請把 app__.py.ui_v2 放到同資料夾）")
        return 2
    if not dst.exists():
        print(f"[ERROR] 找不到 {dst.name}（請把本 patch 放到與 app__.py 同一層）")
        return 2

    bak = backup_file(dst)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    print("[OK] 已套用 UI v2。")
    print(f" - 備份：{bak.name}")
    print(" - 變更：彈窗只顯示中文翻譯 + 播放/停止；點擊外部/滾輪即關閉；無自動關閉計時")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
