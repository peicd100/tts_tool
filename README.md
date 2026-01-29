# TTS翻譯

## (1) 專案簡述
這是一個常駐工作列（系統匣）的 Windows 小工具：當你在任何地方按下 `Ctrl+C` 複製文字後，它會在滑鼠旁邊跳出一個小彈窗，提供：
- 自動翻譯（依內容自動判斷中/英文，決定譯向）
- 一鍵播放（依內容自動判斷中/英文，使用對應語音播放原文）
- 系統匣左鍵點擊可開啟設定：選擇英文/中文語音、啟用/停用（預設不啟用）、結束程式

翻譯採用 `translate.googleapis.com` 的 `client=gtx` 端點（與你原本 JS 版本一致），屬於非官方端點，可能受網路、地區或服務策略影響而失效或被阻擋。

## (2) 重要變數（請勿改名）
- ENV_NAME：`tts_tool`
- EXE_NAME：`TTS翻譯`

> 備註：EXE_NAME 也建議作為 PyInstaller 打包輸出的程式名稱。

## (3) 專案執行方式
在本專案資料夾（與 `app_main.py` 同層）開啟終端機後：

```bash
python app_main.py
```

建議你平常用系統匣常駐執行，若不想要跳出終端機視窗，可用：
- `pythonw app_main.py`（Windows）
- 或打包成 `.exe`（見下方「打包」）

## (4) 專案的目錄結構
（下列為建議放置位置：你提供的路徑可直接將本資料夾內容放進去）

```text
TTS翻譯/
  app_main.py
  app__.py
  README.md
  user_data/
    settings.json
    logs/
      app.log
```

## (5) 設定檔
設定檔位置：`user_data/settings.json`

欄位說明：
- `enabled`：是否啟用（預設 `false`；未啟用時不會因 Ctrl+C 跳出彈窗）
- `voice_en_id`：英文語音 token id（空字串表示自動挑第一個英文聲音）
- `voice_zh_id`：中文語音 token id（空字串表示自動挑第一個中文聲音）
- `popup_auto_hide_ms`：彈窗自動關閉毫秒數（預設 12000）
- `max_chars`：允許觸發彈窗的最大字數（預設 500）
- `translate_timeout_sec`：翻譯請求逾時秒數（預設 6.0）

## (6) log
Log 位置：`user_data/logs/app.log`

## (7) 對外系統連線
- 翻譯：`https://translate.googleapis.com/translate_a/single?client=gtx...`
  - 可能受網路環境、地區或服務策略影響而失效或被阻擋
  - 若你不想使用網路翻譯，可自行替換成離線翻譯或你偏好的翻譯 API

## (8) TODO（可選）
- 讓彈窗同時支援「播放譯文」按鈕
- 彈窗加入「複製譯文」按鈕
- 支援更多快捷鍵（例如 Ctrl+Shift+C 強制顯示）

## (9) 版本資訊
- OS：Windows 11（win-64）
- UI：PySide6（Qt）
- 全域快捷鍵：pynput
- TTS：Windows SAPI（pywin32 / win32com）
- 翻譯：translate.googleapis.com（urllib）

## (10) 快速安裝（Conda / Pip 三選一）
以下三種安裝方式都遵循同一個原則：先建環境，再裝套件。

### A. conda-forge（建議，純 conda）
```bash
conda create -n tts_tool python=3.11 -y
conda activate tts_tool
conda install -c conda-forge pyside6 pynput pywin32 -y
python -c "import sys; import PySide6, pynput; import win32com.client; print('OK', sys.version)"
python app_main.py
```

### B. conda-forge + pip（如果你想額外裝 pyttsx3 做備援）
```bash
conda create -n tts_tool python=3.11 -y
conda activate tts_tool
conda install -c conda-forge pyside6 pynput pywin32 -y
pip install pyttsx3
python -c "import sys; import PySide6, pynput; import win32com.client; import pyttsx3; print('OK', sys.version)"
```

### C. pip-only（不建議，但可用）
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -U pip
pip install PySide6 pynput pywin32
python -c "import sys; import PySide6, pynput; import win32com.client; print('OK', sys.version)"
```

補充：Anaconda 官方也說明了在 conda 環境中必要時可使用 pip 安裝 PyPI 套件，但 conda 不會完全追蹤 pip 裝的相依性，需自行留意相依衝突。

## 打包成 EXE（PyInstaller）
在已安裝相依套件的環境中執行（建議用 A 或 B）：

```bash
conda activate tts_tool
conda install -c conda-forge pyinstaller -y
pyinstaller -F -w -n "TTS翻譯" app_main.py ^
  --clean -y ^
  --hidden-import win32com.client --hidden-import pythoncom
```

輸出位置：`dist/TTS翻譯.exe`

# ========================================
# [凍結區塊] GitHub 操作指令（請勿更動）
# ========================================
# 1) git clone https://github.com/peicd100/tts_tool.git
# 2) cd ENV_NAME
# 3) git checkout -b <branch-name>

# 推送（首推）
# 4) git add .
# 5) git commit -m "init"
# 6) git remote add origin https://github.com/peicd100/tts_tool.git
# 7) git push -u origin <branch-name>

# 之後推送
# 8) git add .
# 9) git commit -m "update"
# 10) git push
# ========================================
