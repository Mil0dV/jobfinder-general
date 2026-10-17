"""Thin launcher so `uv run python cli.py ...` keeps working."""

from jobfinder.cli import main

if __name__ == "__main__":
    main()
