import importlib.util
from pathlib import Path
import sys

from trade_exporter import gui


def entrypoint():
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("diary_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plain_main_opens_the_form_with_the_application_root(monkeypatch):
    module = entrypoint()
    opened = []
    monkeypatch.setattr(sys, "argv", [str(module.ROOT_DIR / "main.py")])
    monkeypatch.setattr(gui, "run_gui", opened.append)
    monkeypatch.setattr(module, "main", lambda: (_ for _ in ()).throw(AssertionError("plain start used console input")))
    module.run()
    assert opened == [module.ROOT_DIR]


def test_command_arguments_keep_the_console_workflow(monkeypatch):
    module = entrypoint()
    ran = []
    monkeypatch.setattr(sys, "argv", [str(module.ROOT_DIR / "main.py"), "--portfolio"])
    monkeypatch.setattr(module, "main", lambda: ran.append(True))
    monkeypatch.setattr(gui, "run_gui", lambda *a: (_ for _ in ()).throw(AssertionError("CLI opened form")))
    module.run()
    assert ran == [True]
