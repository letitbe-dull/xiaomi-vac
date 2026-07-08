"""Pure-tier test config: no homeassistant, runs on native Windows.

The package ``custom_components/xiaomi_vac/__init__.py`` imports homeassistant
at module load, so importing the package would drag HA into the pure tier (which
runs where HA isn't installed). The map modules under test (`map_vector`,
`map_parsers`) have no relative imports at load time, so we put the package
directory on ``sys.path`` and import them standalone instead.
"""
import os
import sys
from types import ModuleType

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

_PKG = os.path.join(
    _ROOT,
    "custom_components", "xiaomi_vac",
)
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

# Synthetic package so modules with relative imports (map.py) can load without
# executing the HA-importing package __init__. Import as `xvac.map`.
if "xvac" not in sys.modules:
    _xvac = ModuleType("xvac")
    _xvac.__path__ = [_PKG]
    sys.modules["xvac"] = _xvac

_TOOLS = os.path.join(_ROOT, ".tools")
tools_pkg = ModuleType("tools")
tools_pkg.__path__ = [_TOOLS]
sys.modules.setdefault("tools", tools_pkg)
