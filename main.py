from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parent

SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))


from trade_exporter.cli import main


def run():
    if len(sys.argv) == 1:
        from trade_exporter.gui import run_gui

        run_gui(ROOT_DIR)
    else:
        main()


if __name__ == "__main__":
    run()
