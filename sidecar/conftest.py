"""Pytest configuration for production_app sidecar tests.

Adds the sidecar directory to sys.path so modules like model_paths,
model_setup, asr_engine etc. are importable without installation.
"""
import sys
from pathlib import Path

# The sidecar directory (where this conftest.py lives) must be on sys.path
# so that `import model_paths` etc. resolves correctly.
SIDECAR_DIR = Path(__file__).resolve().parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))
