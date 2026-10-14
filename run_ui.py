"""Convenient entry point to launch the AI Project Operations Agent UI.

Usage:
    python run_ui.py
"""
import os
import sys

# Ensure root directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.ui import main

if __name__ == "__main__":
    main()

