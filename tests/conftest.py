"""Load the pure-python modules of the integration, with or without Home Assistant."""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "custom_components" / "ha_sport"

if importlib.util.find_spec("homeassistant") is None:
    # Without HA installed, register the package without executing its __init__
    for name, path in (("custom_components", ROOT / "custom_components"), ("custom_components.ha_sport", PKG_DIR)):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = [str(path)]
            sys.modules[name] = mod
