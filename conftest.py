"""Pytest root conftest — ensures the repo root is importable (prepend mode)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
