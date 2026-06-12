#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import platform
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def main():
    print("Python:", sys.version.replace("\n", " "))
    print("Platform:", platform.platform())
    for name in ["dotenv", "PIL", "cv2", "numpy"]:
        try:
            m = importlib.import_module(name)
            print(f"{name}: OK", getattr(m, "__version__", ""))
        except Exception as e:
            print(f"{name}: FAIL -> {e}")
    try:
        import openai
        from openai import OpenAI as _OpenAI  # noqa: F401
        print("openai: OK", getattr(openai, "__version__", ""))
    except Exception as e:
        print(f"openai: FAIL -> {type(e).__name__}: {e}")
    key = os.environ.get("OPENAI_API_KEY", "")
    print("OPENAI_API_KEY:", "SET" if key else "NOT SET")
    if key:
        print("OPENAI_API_KEY preview:", key[:7] + "..." + key[-4:])
    print("Note: this script does not call the API and does not consume credits.")


if __name__ == "__main__":
    main()
