"""
Vercel entry point.

Vercel's Python runtime imports this file and looks for `app`. Everything else
is the real application; this exists so the platform has something to find.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The function bundle puts the repo root one level up from api/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prism.web.api import app  # noqa: E402,F401
