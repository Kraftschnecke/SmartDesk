import unittest

from controller import protocol as p
from controller.protocol import DeskError, ErrorCode, ProtocolError
from controller.transport import LineSplitter


class TestEncodeDecode(unittest.TestCase):
    def test_roundtrip_command(self):
        msg = p.make_command(7, "set_brightness", {"value": 60})
        line = p.encode(msg)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(p.decode(line), msg)

    def test_unknown_fields_are_ignored(self):
        msg = p.decode(b'{"v":1,"type":"PONG","id":3,"status":"READY","extra":42}')
        self.assertEqual(msg["status"], "READY")

    def test_invalid_json(self):
        with self.assertRaises(ProtocolError) as ctx:
            p.decode(b"{kaputt")
        self.assertEqual(ctx.exception.code, ErrorCode.PARSE_ERROR)

    def test_not_an_object(self):
        with self.assertRaises(ProtocolError):
            p.decode(b"[1,2,3]")

    def test_wrong_version(self):
        with self.assertRaises(ProtocolError) as ctx:
            p.decode(b'{"v":2,"type":"PING","id":1}')
        self.assertEqual(ctx.exception.code, ErrorCode.UNSUPPORTED_VERSION)

    def test_unknown_type(self):
        with self.assertRaises(ProtocolError) as ctx:
            p.decode(b'{"v":1,"type":"HELLO","id":1}')
        self.assertEqual(ctx.exception.code, ErrorCode.UNKNOWN_TYPE)

    def test_type_not_allowed_in_direction(self):
        with self.assertRaises(ProtocolError):
            p.decode(p.encode(p.make_ping(1)), allowed_types=p.MANAGER_TYPES)

    def test_missing_fields(self):
        for raw in (b'{"v":1,"type":"RESPONSE"}',            # ohne id
                    b'{"v":1,"type":"EVENT","event":"X"}',    # ohne seq
                    b'{"v":1,"type":"ERROR","id":1}',         # ohne code
                    b'{"v":1,"type":"COMMAND","id":true,"action":"x"}'):  # bool ist keine id
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                p.decode(raw)

    def test_error_without_id_is_allowed(self):
        msg = p.decode(b'{"v":1,"type":"ERROR","id":null,"code":"PARSE_ERROR"}')
        self.assertIsNone(msg["id"])

    def test_too_long(self):
        with self.assertRaises(ProtocolError):
            p.decode(b'{"v":1,"type":"PING","id":1,"x":"' + b"a" * 600 + b'"}')
        with self.assertRaises(ValueError):
            p.encode(p.make_command(1, "x", {"t": "a" * 600}))


class TestValidateParams(unittest.TestCase):
    SPEC = {"value": {"type": "int", "min": 0, "max": 100}}

    def test_ok(self):
        p.validate_params(self.SPEC, {"value": 0})
        p.validate_params(self.SPEC, {"value": 100})
        p.validate_params({"on": "bool"}, {"on": True})

    def test_out_of_range_no_clamping(self):
        with self.assertRaises(DeskError) as ctx:
            p.validate_params(self.SPEC, {"value": 130})
        self.assertEqual(ctx.exception.code, ErrorCode.VALUE_OUT_OF_RANGE)
        self.assertEqual(ctx.exception.details["max"], 100)

    def test_wrong_types(self):
        for params in ({"value": "60"}, {"value": True}, {"value": 1.5}, {}, {"value": 1, "x": 2}):
            with self.subTest(params=params), self.assertRaises(DeskError) as ctx:
                p.validate_params(self.SPEC, params)
            self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PARAMS)
        with self.assertRaises(DeskError):
            p.validate_params({"on": "bool"}, {"on": 1})


class TestLineSplitter(unittest.TestCase):
    def test_split_across_chunks(self):
        s = LineSplitter()
        self.assertEqual(s.feed(b'{"a":'), [])
        self.assertEqual(s.feed(b'1}\r\n{"b":2}\n'), [b'{"a":1}', b'{"b":2}'])

    def test_overlong_line_dropped(self):
        s = LineSplitter(max_len=10)
        self.assertEqual(s.feed(b"x" * 50 + b"\nok\n"), [b"ok"])
        self.assertEqual(s.dropped, 1)


if __name__ == "__main__":
    unittest.main()
