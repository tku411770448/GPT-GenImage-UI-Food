#!/usr/bin/env python3
from __future__ import annotations

try:
    from ui_gpt_defect.app import main
except ImportError as exc:
    msg = str(exc)
    if "PySide6" in msg or "QtCore" in msg or "DLL load failed" in msg:
        print("[ERROR] PySide6/Qt 匯入失敗。")
        print("請確認已在專案 .venv 中安裝 requirements.txt：")
        print(r"  python -m venv .venv")
        print(r"  .\.venv\Scripts\activate   # Windows")
        print(r"  source .venv/bin/activate    # Linux/macOS")
        print(r"  python -m pip install --upgrade pip")
        print(r"  python -m pip install -r requirements.txt")
        print(r"  python launch_ui.py")
        raise SystemExit(1) from exc
    raise

if __name__ == "__main__":
    main()
