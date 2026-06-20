"""Download all production models into sidecar/models (dev convenience).

Run from the repo root:  python scripts/download_models.py
"""
import sys
from pathlib import Path

# Make the sidecar package importable.
SIDE = Path(__file__).resolve().parent.parent / "sidecar"
sys.path.insert(0, str(SIDE))

import model_setup  # noqa: E402


def _print(ev: dict) -> None:
    e = ev.get("event")
    if e == "setup_step":
        print(f"[{ev['index']}/{ev['total']}] {ev.get('label','')}: {ev['name']}")
    elif e == "model_progress":
        sys.stdout.write(
            f"\r    {ev['downloaded_mb']:.1f}/{ev['total_mb']:.1f} MB ({ev['pct']:.1f}%)"
        )
        sys.stdout.flush()
    elif e == "model_done":
        print("  done")
    elif e == "setup_done":
        print("All models ready.")
    elif e == "setup_error":
        print("\nERROR:", ev.get("message"))


if __name__ == "__main__":
    model_setup.download_all(_print)
