from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent

SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))


from trade_exporter.cli import main


if __name__ == "__main__":
    main()
