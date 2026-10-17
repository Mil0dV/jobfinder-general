"""Project-root location, shared by every module that reads or writes data files."""

from pathlib import Path

# src/jobfinder/config/paths.py -> repository root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
