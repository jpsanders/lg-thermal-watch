"""LG Thermal Watch: a night watch, a temperature log and a dashboard for an LG heat pump."""

from lg_thermal_watch.agent import LGPC18SKAgent
from lg_thermal_watch.controller import ThinQAgentController
from lg_thermal_watch.nightwatch import LGPC18SKNightWatch
from lg_thermal_watch.tempdb import RoomTempLogger

__all__ = [
    "LGPC18SKAgent",
    "LGPC18SKNightWatch",
    "RoomTempLogger",
    "ThinQAgentController",
]

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("lg-thermal-watch")
except PackageNotFoundError:  # running from a source tree, as the systemd unit in the README does
    __version__ = "0.0.0+source"
