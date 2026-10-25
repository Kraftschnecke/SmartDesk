"""Controller + simulierter LightManager – alle Abläufe aus dem Protokoll."""

import unittest

from controller.protocol import DeskError, ErrorCode, Status

from test.helpers import LightSetup, wait_until


class LightTestCase(unittest.TestCase):
    def setUp(self):
        self.s = LightSetup().start()
        self.c = self.s.controller

    def tearDown(self):
        self.s.stop()


class TestHandshake(LightTestCase):
    def test_handshake_fills_desk_state(self):
        info = self.c.desk_state.manager("light")
        self.assertEqual(info.status, Status.READY)
        self.assertEqual(info.identity["manager"], "light")
        self.assertIn("set_brightness", info.capabilities["actions"])
        self.assertEqual(info.state, {"power": False, "brightness": 50})


class TestCommands(LightTestCase):
    def test_set_brightness_returns_actual_state(self):
        result = self.c.execute("light", "set_brightness", {"value": 60})
        self.assertEqual(result, {"power": False, "brightness": 60})
        self.assertEqual(self.s.state()["brightness"], 60)
        self.assertEqual(self.s.sim.brightness, 60)

    def test_set_power(self):
        self.c.execute("light", "set_power", {"on": True})
        self.assertTrue(self.s.state()["power"])
        self.assertTrue(self.s.sim.power)

    def test_out_of_range_is_rejected_before_sending(self):
        before = self.s.sim.commands_received
        with self.assertRaises(DeskError) as ctx:
            self.c.execute("light", "set_brightness", {"value": 130})
        self.assertEqual(ctx.exception.code, ErrorCode.VALUE_OUT_OF_RANGE)
        self.assertEqual(self.s.sim.commands_received, before)
        self.assertEqual(self.s.state()["brightness"], 50)

    def test_manager_checks_again(self):
        # Am Controller vorbei direkt an den Manager: der prüft trotzdem selbst.
        with self.assertRaises(DeskError) as ctx:
            self.s.link.command("set_brightness", {"value": -5})
        self.assertEqual(ctx.exception.code, ErrorCode.VALUE_OUT_OF_RANGE)
        self.assertEqual(self.s.sim.brightness, 50)

    def test_unknown_action_and_manager(self):
        with self.assertRaises(DeskError) as ctx:
            self.c.execute("light", "explode")
        self.assertEqual(ctx.exception.code, ErrorCode.UNKNOWN_ACTION)
        with self.assertRaises(DeskError) as ctx:
            self.c.execute("motor", "move")
        self.assertEqual(ctx.exception.code, ErrorCode.UNKNOWN_MANAGER)


class TestAdjust(LightTestCase):
    def test_no_wrap_around(self):
        self.c.execute("light", "set_brightness", {"value": 1})
        self.c.execute("light", "adjust_brightness", {"delta": -10})   # nur -1 möglich
        self.assertEqual(self.s.state()["brightness"], 0)
        with self.assertRaises(DeskError) as ctx:
            self.c.execute("light", "adjust_brightness", {"delta": -10})  # keine Änderung möglich
        self.assertEqual(ctx.exception.code, ErrorCode.NO_CHANGE_POSSIBLE)

    def test_adjust_up_to_max(self):
        self.c.execute("light", "set_brightness", {"value": 95})
        self.c.execute("light", "adjust_brightness", {"delta": 20})
        self.assertEqual(self.s.state()["brightness"], 100)


class TestEvents(LightTestCase):
    def test_button_goes_through_controller(self):
        self.s.sim.press_button()
        self.assertTrue(wait_until(lambda: self.s.sim.power is True))
        self.assertTrue(wait_until(lambda: self.s.state()["power"] is True))
        self.s.sim.press_button()
        self.assertTrue(wait_until(lambda: self.s.sim.power is False))

    def test_estop_flow(self):
        self.c.execute("light", "set_power", {"on": True})
        self.s.sim.set_estop(True)
        self.assertTrue(wait_until(lambda: self.s.status() == Status.EMERGENCY))
        self.assertFalse(self.s.state()["power"])      # Ist-Zustand: aus

        with self.assertRaises(DeskError) as ctx:
            self.c.execute("light", "set_power", {"on": True})
        self.assertEqual(ctx.exception.code, ErrorCode.ESTOP_ACTIVE)

        with self.assertRaises(DeskError) as ctx:         # noch gedrückt -> Reset geht nicht
            self.c.reset("light")
        self.assertEqual(ctx.exception.code, ErrorCode.ESTOP_ACTIVE)

        self.s.sim.set_estop(False)
        self.assertTrue(wait_until(lambda: any(e["event"] == "ESTOP_RELEASED" for e in self.c.events)))
        self.assertEqual(self.s.status(), Status.EMERGENCY)  # kein automatischer Neustart

        self.c.reset("light")
        self.assertEqual(self.s.status(), Status.READY)
        self.assertFalse(self.s.state()["power"])          # bleibt aus nach dem Reset
        self.c.execute("light", "set_power", {"on": True})
        self.assertTrue(self.s.sim.power)


class TestConnection(LightTestCase):
    def test_timeout_and_offline_and_recovery(self):
        self.s.sim.frozen = True
        with self.assertRaises(DeskError) as ctx:
            self.c.execute("light", "set_brightness", {"value": 70})
        self.assertEqual(ctx.exception.code, ErrorCode.TIMEOUT)
        self.assertTrue(wait_until(lambda: self.s.status() == Status.OFFLINE))

        with self.assertRaises(DeskError) as ctx:
            self.c.execute("light", "set_brightness", {"value": 70})
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_CONNECTED)

        self.s.sim.frozen = False
        self.assertTrue(wait_until(lambda: self.s.status() == Status.READY))

    def test_reboot_takes_actual_state_and_sends_no_old_commands(self):
        self.c.execute("light", "set_brightness", {"value": 80})
        self.c.execute("light", "set_power", {"on": True})
        before = self.s.sim.commands_received
        self.s.sim.boot()   # Manager startet neu, Zustand zurückgesetzt
        self.assertTrue(wait_until(lambda: self.s.state() == {"power": False, "brightness": 50}))
        self.assertTrue(wait_until(lambda: self.s.status() == Status.READY))
        self.assertEqual(self.s.sim.commands_received, before)

    def test_garbage_from_manager_is_ignored(self):
        self.s.sim_end.send_line(b"das ist kein json\n")
        self.s.sim_end.send_line(b'{"v":1,"type":"RESPONSE","id":99999,"data":{}}\n')  # unbekannte id
        self.assertTrue(wait_until(lambda: self.s.link.bad_lines == 1))
        result = self.c.execute("light", "set_brightness", {"value": 33})
        self.assertEqual(result["brightness"], 33)

    def test_lost_events_are_counted(self):
        self.s.sim._seq += 2   # zwei Events "verloren"
        self.s.sim.press_button()
        self.assertTrue(wait_until(lambda: self.s.link.lost_events == 2))


class TestControllerLost(unittest.TestCase):
    def test_manager_notices_missing_controller(self):
        s = LightSetup(controller_timeout=0.3).start()
        try:
            s.sim.set_estop(False)
            s.ctrl_end.cut = True       # Leitung unterbrochen
            self.assertTrue(wait_until(lambda: s.sim.status == Status.CONTROLLER_LOST))
            s.ctrl_end.cut = False
            self.assertTrue(wait_until(lambda: s.sim.status == Status.READY))
            self.assertTrue(wait_until(lambda: s.status() == Status.READY))
        finally:
            s.stop()


if __name__ == "__main__":
    unittest.main()
