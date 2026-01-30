# TTS翻譯

## (1) 專案概述

本專案是一個 Windows 11 常駐系統匣（工作列右下角）的桌面小工具：**啟用後**，當你在任何地方複製文字（通常是 `Ctrl+C`，也包含右鍵/選單複製）且剪貼簿文字變更時，會彈出小視窗，提供：

* **即時翻譯**：

  * 原文為英文（偵測到英文比例較高）時，翻譯成繁體中文（`zh-TW`）。
  * 原文為中文（偵測到中文比例較高）時，**不翻譯**，直接顯示原文。
* **彈窗只顯示「中文內容」**：只顯示繁中翻譯（或中文原文）+「播放/停止」按鈕（不顯示英文原文）。
* **一鍵播放原文（TTS）**：朗讀內容為「原文」，自動判斷中/英文並用對應語音播放（Windows SAPI；需 `pywin32`）。
* **關閉彈窗（全域）**：點擊彈窗外任意位置或滾輪滾動即可關閉（需 `pynput`）。
* **系統匣左鍵**：開啟設定。
* **系統匣右鍵**：快速切換啟用、開啟設定、測試彈窗、結束。

翻譯使用 `translate.googleapis.com` 的 `client=gtx` 端點（非官方；可能受網路/地區/服務策略影響而失效或被阻擋）。

## (2) 重要變數（必填）

* ENV_NAME：`tts_tool`
* EXE_NAME：`TTS翻譯`

## (3) workspace root 定義

本 README 所稱 **workspace root** 指本專案根目錄（`README.md` 必須位於此目錄下）。

所有指令請在 workspace root（與 `app_main.py` 同層）開啟終端機執行。

## (4) 檔案與資料夾結構（樹狀；最小必要集合）

```text
TTS翻譯
├─ app_main.py
├─ app__.py
├─ README.md
└─ user_data
   ├─ settings.json
   └─ logs
      └─ app.log
```

* 專案輸入檔案存放位置：**無**（輸入來源為「剪貼簿文字」）。
* 專案輸出檔案存放位置：`user_data/`

  * `user_data/settings.json`：設定檔（不包含啟用狀態 enabled）
  * `user_data/logs/app.log`：log

## (5) Python 檔名規則

本專案包含 Python 入口點，必須遵守：**`app_main.py` + `app__.py` 同層**。

* `app_main.py`：入口點（僅負責呼叫 `app__` 的 `main()`）
* `app__.py`：主程式（Tray / Clipboard / Popup / Translate / TTS）

## (6) user_data/ 規範（所有輸入/輸出/設定預設放在 user_data/）

* 預設所有「可持久化」資料皆放在 `user_data/`，避免污染原始碼目錄：

  * 設定：`user_data/settings.json`
  * 日誌：`user_data/logs/app.log`
* **啟用狀態（enabled）屬於 runtime 狀態：每次啟動固定為「不啟用」且不落盤**。
* 開發（非 frozen）模式：`user_data/` 位於 workspace root 下。
* 打包（frozen）模式：`user_data/` 預設位於 `.exe` 同層（`<EXE_DIR>/user_data/`）。
* 首次執行若缺少 `user_data/` 或其子目錄，程式會自動建立。

## (7) Conda 環境（ENV_NAME）規範

* 僅允許建立/修改 **ENV_NAME=tts_tool** 對應的 conda 環境。
* 禁止改動任何其他 conda 環境（包含 `base`）。
* 工具鏈與套件一律優先使用 conda（尤其涉及 GUI：PySide6 必須以 conda-forge 安裝）。

## (8) 從零開始安裝流程（可一鍵複製）

以下命令預設在 **Anaconda Prompt（Windows CMD）** 執行。

### A. 推薦方案（conda-forge；成功率最高）

（本方案使用 conda-forge 安裝 PySide6 與 pywin32；無需 pip。）

```bat
conda create -n tts_tool python=3.11 -y
conda activate base && conda activate tts_tool
conda install -c conda-forge pyside6 pywin32 pynput pyinstaller -y
python -c "import sys; print(sys.executable); print(sys.prefix); import PySide6; import win32com.client; import pynput; import PyInstaller; print('OK')"
```

執行程式（與上方安裝指令分離；不可併入第 5 行）：

```bat
python app_main.py
```

### B. 備援方案（conda + pip；僅在 conda-forge 某套件不可用/失敗時使用）

（若你遇到 conda-forge 安裝 `pywin32` 失敗或版本不相容，可改用 pip 補裝。這是備援，不是常態。）

```bat
conda create -n tts_tool python=3.11 -y
conda activate base && conda activate tts_tool
conda install -c conda-forge pyside6 pynput pyinstaller -y
pip install pywin32
python -c "import sys; print(sys.executable); print(sys.prefix); import PySide6; import win32com.client; import pynput; import PyInstaller; print('OK')"
```

執行程式：

```bat
python app_main.py
```

## (9) 測試方式

1. 執行 `python app_main.py` 後，確認右下角系統匣出現「TTS翻譯」圖示。
2. **預設未啟用**：請先右鍵系統匣圖示，勾選「啟用」。
3. 打開任意應用程式（記事本/瀏覽器等），選取一段文字按 `Ctrl+C`。
4. 應彈出視窗（位置依設定：滑鼠旁邊 / 顯示器正中心）：

   * 初始顯示「翻譯中…」
   * 翻譯完成後：只顯示繁中翻譯（或中文原文），不顯示英文原文
   * 按「播放」會朗讀**原文**（需 Windows SAPI + `pywin32`）
5. 點擊彈窗外任意位置或滾輪滾動可關閉（需 `pynput`）。

### Troubleshooting

* **按 Ctrl+C 沒反應**

  * 確認系統匣右鍵選單「啟用」已勾選（每次啟動預設未啟用）。
  * 確認剪貼簿內容是「文字」且長度未超過設定的「最長字數」。
* **彈窗顯示「翻譯中…」很久或顯示「（翻譯失敗或被阻擋）」**

  * 可能是網路問題、逾時、或 `translate.googleapis.com` 端點被阻擋/限流。
  * 可在設定中提高「翻譯逾時」或改用不同網路環境測試。
* **無法用點擊外部/滾輪關閉彈窗**

  * 請確認已安裝 `pynput`。
* **播放按鈕不可用**

  * 請確認已安裝 `pywin32`，並且 Windows 有可用語音（SAPI voices）。
  * 可用系統匣左鍵開設定，選擇英文/中文語音。
* **全域快捷鍵無作用**

  * 快捷鍵只在「已啟用」狀態下生效。
  * 請確認已安裝 `pynput`（全域快捷鍵依賴 `pynput.keyboard`）。

## (10) 打包成 .exe（必填；提供可複製指令）

以下以 PyInstaller 打包為單一可攜 `.exe`。

注意：PyInstaller 會產生/覆寫 `dist/` 與 `build/`（屬 build artifacts）。若你已有人為保留的內容，請先自行備份再執行。

```bat
conda activate base && conda activate tts_tool
pyinstaller -F -w -n "TTS翻譯" app_main.py -y --clean --hidden-import win32com.client --hidden-import pythoncom
```

輸出位置：`dist\TTS翻譯.exe`

可攜式持久化（重要）：

* 發佈時請確保 `.exe` 同層存在 `user_data/`（`dist/user_data/`），否則程式會在可寫入時自動建立。
* `user_data/` 為持久化資料夾，**不應被嵌入 `.exe`**，也不應放在 onefile 暫存解壓目錄。

## (11) 使用者要求（必填；長期約束；需持續維護）

* 只要程式需要 GUI/視窗，介面層一律使用 **PySide6**（不得改用 Tkinter/PyQt/Electron 等）。
* 只要涉及 PySide6，**conda 安裝必須指定 conda-forge**；只有在 conda-forge 明確不可行且有可重現證據時，才允許改用 pip。
* 深色主題固定要求：

  * 全域 accent color：`#72e3fd`
  * Window/Background：`#0f141a`
  * Surface/Card：`#141b2d`
  * Text：`#e8eef4`
* 顏色必須「單一來源」集中管理（例如統一放在 `app__.py` 的 Theme 常數），禁止在多處散落硬編碼。
* 任何改動入口點/安裝方式/輸入輸出/設定位置/GUI 框架規則時，必須同步更新本 README 相關章節並保持一致。

## (12) GitHub操作指令（必填；必須置於 README.md 最後面；凍結區塊）

# 初始化

```
(
echo.
echo # ignore build outputs
echo dist/
echo build/
)>> .gitignore
git init
git branch -M main
git remote add origin https://github.com/peicd100/tts_tool.git
git add .
git commit -m "PEICD100"
git push -u origin main
```

# 例行上傳

```
git add .
git commit -m "PEICD100"
git push -u origin main
```

# 還原成Git Hub最新資料

```
git rebase --abort || echo "No rebase in progress" && git fetch origin && git switch main && git reset --hard origin/main && git clean -fd && git status
```

# 查看儲存庫

```
git remote -v
```

# 克隆儲存庫

```
git clone https://github.com/peicd100/tts_tool.git
```
