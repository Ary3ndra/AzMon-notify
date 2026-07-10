"""Plugin package. `discover()` imports every plugin module in this folder so
their @register runs — drop a .py in here (or upload via the UI) and it appears.
"""
from .base import (Plugin, PluginContext, build_plugins,  # noqa: F401
                   discover, register)

discover()
