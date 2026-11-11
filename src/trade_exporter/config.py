from pathlib import Path
import sys
import tomllib


def load_config(path: str | Path = "config.toml") -> dict:
    path = Path(path)
    if not path.exists() and path.name == "config.toml" and getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "config.toml"
        if bundled.is_file():
            path = bundled
    if not path.exists():
        raise FileNotFoundError(f"Не найден config.toml: {path.resolve()}")
    with path.open("rb") as file:
        return tomllib.load(file)
