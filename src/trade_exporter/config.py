from pathlib import Path
import tomllib


def load_config(path: str | Path = "config.toml") -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Не найден config.toml: {path.resolve()}")
    with path.open("rb") as file:
        return tomllib.load(file)
