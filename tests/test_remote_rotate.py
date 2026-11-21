"""Turning a cloud iPhone (PHO-337).

A cloud iPhone lies flat in a rack and cannot be stood up, so which way up its
screen is has to be asked for. The control host here is a stand-in; no cloud.
It answers a turn as phone-host does, {device, orientation, turned}, and
refuses one as the control URL passes it on: the host's sentence, no code.
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from phone_harness import transport  # noqa: E402
from phone_harness.remote import Remote  # noqa: E402
from phone_harness.transport import Unsupported  # noqa: E402

TALL, WIDE = {"x": 0, "y": 0, "w": 750, "h": 1334, "id": "udid1"}, {"x": 0, "y": 0, "w": 1334, "h": 750, "id": "udid1"}
HOST_UPRIGHT = "OrientationLocked: Portrait Orientation Lock is on: the phone did not turn"


class Control(BaseHTTPRequestHandler):
    """A phone's control URL: /ops and /op, as phone-host serves them."""
    ops = ()
    orientation = "portrait"
    app_turns = True        # False: the app in front supports one way up and stays upright
    upright_lock = False    # iOS's Portrait Orientation Lock
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.headers.get("Authorization") != "Bearer ct_1":
            return self._send(404, {"error": "not found"})
        if self.path == "/ops":
            return self._send(200, {"ops": sorted(Control.ops)})
        if self.path == "/op":
            Control.calls.append(body)
            op, kw = body["op"], body.get("kw") or {}
            if op not in Control.ops:
                return self._send(400, {"error": f"this phone cannot {op!r}", "unsupported": True})
            if op == "device.rotate":
                if set(kw) != {"to"} or kw["to"] not in ("portrait", "landscape_left", "landscape_right"):
                    return self._send(400, {"error": "to must be portrait, landscape_left or landscape_right"})
                if Control.upright_lock and kw["to"] != Control.orientation:
                    return self._send(409, {"error": HOST_UPRIGHT})
                turned = 0 if kw["to"] == Control.orientation else 1
                Control.orientation = kw["to"]
                return self._send(200, {"result": {"device": kw["to"], "turned": turned,
                                                   "orientation": kw["to"] if Control.app_turns else "portrait"}})
            if op == "session.state":
                return self._send(200, {"result": "ready"})
            if op == "screen.capture":
                wide = Control.orientation != "portrait" and Control.app_turns
                return self._send(200, {"result": {"png_b64": "", "bounds": WIDE if wide else TALL}})
            if op == "screen.bounds":
                return self._send(200, {"result": TALL})        # the size the session started with
            return self._send(200, {"result": True})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = _handle


class TurningACloudIphone(unittest.TestCase):
    def setUp(self):
        Control.ops = ("screen.bounds", "screen.capture", "session.state", "input.tap", "device.rotate", "device.wake")
        Control.orientation, Control.app_turns, Control.upright_lock, Control.calls = "portrait", True, False, []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.remote = Remote(f"http://127.0.0.1:{server.server_port}", "ct_1")

    def test_the_vocabulary_has_the_op_and_a_host_that_lists_it_supports_it(self):
        self.assertIn("device.rotate", transport.OP_NAMES)
        self.assertTrue(self.remote.supports("device.rotate"))
        self.assertIn("device.rotate", self.remote.ops())

    def test_each_way_up_is_one_op_and_the_two_landscapes_are_different_words(self):
        for to in ("landscape_left", "landscape_right", "portrait"):
            self.assertEqual(self.remote.send("device.rotate", to=to), {"device": to, "orientation": to, "turned": 1})
        self.assertEqual(Control.calls, [{"op": "device.rotate", "kw": {"to": "landscape_left"}},
                                         {"op": "device.rotate", "kw": {"to": "landscape_right"}},
                                         {"op": "device.rotate", "kw": {"to": "portrait"}}])

    def test_a_capture_follows_the_turn_and_the_reported_size_may_not(self):
        """phone-host records the screen's size once, when the session becomes ready."""
        self.remote.send("device.rotate", to="landscape_left")
        self.assertEqual(self.remote._op("screen.capture")["bounds"]["w"], 1334)
        self.assertEqual(self.remote.send("screen.bounds")["w"], 750)

    def test_an_app_that_does_not_turn_leaves_the_screen_drawn_upright(self):
        Control.app_turns = False
        self.assertEqual(self.remote.send("device.rotate", to="landscape_left"),
                         {"device": "landscape_left", "orientation": "portrait", "turned": 1})

    def test_portrait_orientation_lock_is_said_in_the_hosts_own_words(self):
        """The refusal is a 409 like the locked one, so the phone is asked its state, and it is not locked."""
        Control.upright_lock = True
        with self.assertRaises(RuntimeError) as raised:
            self.remote.send("device.rotate", to="landscape_left")
        self.assertEqual(str(raised.exception), HOST_UPRIGHT)
        self.assertEqual([c["op"] for c in Control.calls], ["device.rotate", "session.state"])

    def test_a_host_that_cannot_turn_a_phone_is_unsupported_and_is_sent_nothing(self):
        Control.ops = ("screen.bounds", "input.tap", "device.wake", "session.state")
        self.assertFalse(self.remote.supports("device.rotate"))
        with self.assertRaises(Unsupported) as raised:
            self.remote.send("device.rotate", to="portrait")
        self.assertEqual(str(raised.exception), "this cloud phone cannot 'device.rotate'")
        self.assertEqual(Control.calls, [])


class TheRotateHelper(unittest.TestCase):
    class Phone:
        def __init__(self, ops):
            self.ops, self.sent = set(ops), []

        def supports(self, op):
            return op in self.ops

        def send(self, op, **kw):
            self.sent.append((op, kw))
            return {"device": kw.get("to"), "orientation": kw.get("to"), "turned": 1}

    def with_phone(self, ops):
        from phone_harness import helpers
        phone = self.Phone(ops)
        saved = helpers.phone
        helpers.phone = phone
        self.addCleanup(setattr, helpers, "phone", saved)
        return helpers, phone

    def test_rotate_sends_the_op_with_the_way_up(self):
        helpers, phone = self.with_phone({"device.rotate"})
        for to in ("portrait", "landscape_left", "landscape_right"):
            self.assertEqual(helpers.rotate(to), {"device": to, "orientation": to, "turned": 1})
        self.assertEqual(phone.sent, [("device.rotate", {"to": "portrait"}), ("device.rotate", {"to": "landscape_left"}),
                                      ("device.rotate", {"to": "landscape_right"})])

    def test_anything_else_is_refused_here_and_nothing_is_sent(self):
        helpers, phone = self.with_phone({"device.rotate"})
        for to in ("landscape", "upside_down", "left", "LANDSCAPE_LEFT", "landscape-left", "", None, 90, True):
            with self.subTest(to=to), self.assertRaises(ValueError) as raised:
                helpers.rotate(to)
            self.assertIn("'portrait', 'landscape_left' or 'landscape_right'", str(raised.exception))
        self.assertEqual(phone.sent, [])

    def test_a_phone_that_cannot_be_turned_says_cloud_iphones_only_and_sends_nothing(self):
        helpers, phone = self.with_phone({"nav.home", "input.tap"})
        with self.assertRaises(Unsupported) as raised:
            helpers.rotate("landscape_left")
        self.assertIn("cloud iPhones only", str(raised.exception))
        self.assertEqual(phone.sent, [])

    def test_no_local_backend_claims_to_turn_a_phone(self):
        from phone_harness.android import Android
        self.assertIsNone(getattr(Android, "_device_rotate", None))
        if sys.platform == "darwin":
            try:
                from phone_harness.ios import IPhone
            except ImportError:
                return
            self.assertIsNone(getattr(IPhone, "_device_rotate", None))


if __name__ == "__main__":
    unittest.main()
