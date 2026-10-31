"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def core():
    """The shared trader_core package (ROOT is importable from this module on)."""
    import trader_core

    return trader_core


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
