"""Launch the offline sample from the project directory."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from business_data.pipeline import main

if __name__ == "__main__":
    raise SystemExit(main())
