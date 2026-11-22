"""
Entry point utama untuk LoveYou Modular Service & Device Agent.
Mendukung eksekusi langsung (`python backend/main.py`) maupun paket (`python -m backend.main`).
"""

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
for _p in (_BACKEND_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from backend.screenshot_service import main
except ImportError:
    from screenshot_service import main


if __name__ == "__main__":
    main()
