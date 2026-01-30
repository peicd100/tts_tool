# -*- coding: utf-8 -*-
"""
patch_remember_except_enabled.py

目標：
- 記住所有設定（字體大小/每行字數/位置/語音/翻譯逾時/最長字數...）
- 唯獨不記住「啟用」（每次啟動 runtime 預設不啟用）

做法：
1) SettingsStore.load(): 忽略 enabled，其餘照 settings.json 套回
2) SettingsStore.save(): merge 寫入 + 不寫 enabled
3) 若 set_enabled() 內有 store.save(...) 則移除，避免用舊 settings 覆寫檔案

用法：
  python patch_remember_except_enabled.py --dry-run
  python patch_remember_except_enabled.py
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path


def die(msg: str, code: int = 2) -> int:
    print(f"[ERROR] {msg}")
    return code


def find_class_start(lines: list[str], class_name: str) -> int | None:
    pat = re.compile(rf"^class\s+{re.escape(class_name)}\s*[:\(]")
    for i, line in enumerate(lines):
        if pat.search(line):
            return i
    return None


def find_method_block(lines: list[str], start_from: int, method_name: str) -> tuple[int, int] | None:
    pat = re.compile(rf"^    def\s+{re.escape(method_name)}\s*\(")
    i = None
    for idx in range(start_from, len(lines)):
        if pat.search(lines[idx]):
            i = idx
            break
    if i is None:
        return None

    end = i + 1
    while end < len(lines):
        line = lines[end]
        if line.startswith("    def "):
            break
        if line.startswith("    @"):
            break
        if re.match(r"^class\s+", line):
            break
        end += 1
    return i, end


def remove_store_save_inside_set_enabled(lines: list[str]) -> tuple[list[str], int]:
    changed = 0
    search_from = 0
    while True:
        blk = find_method_block(lines, search_from, "set_enabled")
        if not blk:
            break
        s, e = blk
        block = lines[s:e]
        new_block: list[str] = []
        for ln in block:
            if ".store.save(" in ln or re.search(r"\.save\(\s*self\.(settings|_s)\s*\)", ln):
                changed += 1
                continue
            new_block.append(ln)
        lines = lines[:s] + new_block + lines[e:]
        search_from = s + len(new_block)
    return lines, changed


def patch_settingsstore_load_save(lines: list[str]) -> tuple[list[str], bool, bool]:
    cls_start = find_class_start(lines, "SettingsStore")
    if cls_start is None:
        return lines, False, False

    ok_load = False
    ok_save = False

    # 置換 load
    blk = find_method_block(lines, cls_start, "load")
    if blk:
        s, e = blk
        new_load = [
            "    def load(self) -> Settings:",
            "        with self._lock:",
            "            # enabled 不讀取：每次啟動 runtime 一律用 DEFAULT_ENABLED_RUNTIME",
            "            s = Settings(enabled=DEFAULT_ENABLED_RUNTIME)",
            "            try:",
            "                if not self.path.exists():",
            "                    return s",
            "                data = json.loads(self.path.read_text(encoding='utf-8'))",
            "                if isinstance(data, dict):",
            "                    for k, v in data.items():",
            "                        if k == 'enabled':",
            "                            continue",
            "                        if hasattr(s, k):",
            "                            setattr(s, k, v)",
            "                s.enabled = DEFAULT_ENABLED_RUNTIME",
            "                # 防呆：位置模式不合法就回預設（若有此欄位）",
            "                if hasattr(s, 'popup_position_mode'):",
            "                    if getattr(s, 'popup_position_mode') not in (POS_CURSOR, POS_WINDOW_CENTER):",
            "                        setattr(s, 'popup_position_mode', DEFAULT_POSITION_MODE)",
            "                return s",
            "            except Exception:",
            "                return s",
            "",
        ]
        lines = lines[:s] + new_load + lines[e:]
        ok_load = True

    # 置換 save（merge 寫入 + 不寫 enabled）
    blk = find_method_block(lines, cls_start, "save")
    if blk:
        s, e = blk
        new_save = [
            "    def save(self, s: Settings) -> None:",
            "        with self._lock:",
            "            self.path.parent.mkdir(parents=True, exist_ok=True)",
            "",
            "            # merge 寫入：保留既有設定欄位，只更新新值；enabled 永遠不落盤",
            "            existing = {}",
            "            try:",
            "                if self.path.exists():",
            "                    raw = self.path.read_text(encoding='utf-8')",
            "                    obj = json.loads(raw)",
            "                    if isinstance(obj, dict):",
            "                        existing = obj",
            "            except Exception:",
            "                existing = {}",
            "",
            "            data = asdict(s)",
            "            data.pop('enabled', None)",
            "            existing.update(data)",
            "",
            "            self.path.write_text(",
            "                json.dumps(existing, ensure_ascii=False, indent=2),",
            "                encoding='utf-8'",
            "            )",
            "",
        ]
        lines = lines[:s] + new_save + lines[e:]
        ok_save = True

    return lines, ok_load, ok_save


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    if Path.cwd().resolve() != here:
        return die("請先 cd 到此腳本所在資料夾（與 app__.py 同層）再執行。")

    app_py = here / "app__.py"
    if not app_py.exists():
        return die("找不到 app__.py（請把本腳本放在 app__.py 同一層）。")

    text = app_py.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    # 1) set_enabled 內若有 store.save()，移除（避免覆寫 settings.json）
    lines, removed = remove_store_save_inside_set_enabled(lines)

    # 2) SettingsStore.load/save 修成：記住所有設定、唯獨不記住 enabled
    lines, ok_load, ok_save = patch_settingsstore_load_save(lines)

    new_text = "\n".join(lines) + "\n"

    if new_text == text:
        print("[INFO] 沒有偵測到可套用的變更（可能已符合需求或檔案結構不同）。")
        return 0

    # 備份
    backup_dir = here / "user_data" / "patch_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = backup_dir / f"app__.py.bak.remember_except_enabled.{ts}"

    print("[PLAN] 將修改 app__.py：")
    print(f"  - 備份：{bak}")
    print(f"  - set_enabled 內移除 store.save 行數：{removed}")
    print(f"  - SettingsStore.load 置換：{'是' if ok_load else '否（找不到 load）'}")
    print(f"  - SettingsStore.save 置換：{'是' if ok_save else '否（找不到 save）'}")

    # 基本防呆：新 save/load 需要 json + asdict
    src = "\n".join(lines)
    if "import json" not in src:
        print("[WARN] app__.py 內缺 import json，請確認原檔是否被改壞。")

    if "asdict" not in src:
        print("[WARN] app__.py 內缺 asdict（dataclasses），請確認原檔是否被改壞。")

    if args.dry_run:
        print("[DRY-RUN] 未寫入任何檔案。")
        return 0

    bak.write_bytes(app_py.read_bytes())
    app_py.write_text(new_text, encoding="utf-8")
    print("[OK] 已套用：除了「啟用」以外的設定都會記住；啟用狀態每次啟動固定不啟用。")
    print("      建議驗證：改字體/行寬/位置→儲存→關閉設定→重開設定，數值應保留；重啟程式後『啟用』應回到未勾選。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
