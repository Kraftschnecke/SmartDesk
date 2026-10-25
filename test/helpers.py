"""Gemeinsame Hilfen für die Tests (kein Test selbst)."""

from __future__ import annotations

import time
from typing import Callable

from controller.controller import DeskController
from controller.manager_link import ManagerLink
from controller.simulated_light import SimulatedLightManager
from controller.transport import MemoryTransport

# Kurze Zeiten, damit die Tests schnell laufen (Verhältnisse wie im Protokoll)
FAST = dict(request_timeout=0.2, ping_interval=0.05, missed_pings_offline=3)


def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class LightSetup:
    """Controller + simulierter LightManager, über eine Speicher-Verbindung verbunden."""

    def __init__(self, controller_timeout: float = 3.0):
        ctrl_end, sim_end = MemoryTransport.pair()
        self.ctrl_end, self.sim_end = ctrl_end, sim_end
        self.controller = DeskController()
        self.link = ManagerLink("light", ctrl_end, self.controller.desk_state, **FAST)
        self.controller.add_link(self.link)
        self.sim = SimulatedLightManager(sim_end, controller_timeout=controller_timeout)

    def start(self) -> "LightSetup":
        self.controller.start()
        self.sim.start()
        assert wait_until(lambda: self.status() == "READY"), f"nicht READY, sondern {self.status()}"
        return self

    def stop(self) -> None:
        self.sim.stop()
        self.controller.stop()

    def status(self) -> str:
        return self.controller.desk_state.manager("light").status

    def state(self) -> dict:
        return self.controller.desk_state.manager("light").state
