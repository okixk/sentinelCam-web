"""Wire-format + H.264 helper tests for the streaming relay.

Modules are loaded directly by file path so these run without the app's heavy
runtime deps (fastapi/argon2/asyncpg). The golden header vector below is the
cross-repo contract: the matching test in sentinelCam-worker asserts the SAME
bytes, so either side drifting fails its own suite.
"""
import asyncio
import importlib.util
import pathlib
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # register before exec so @dataclass can resolve __module__
    spec.loader.exec_module(module)
    return module


proto = _load("app/streaming/protocol.py", "sc_proto")
h264 = _load("app/streaming/h264.py", "sc_h264")
hub_mod = _load("app/streaming/hub.py", "sc_hub")


class ProtocolContractTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(proto.PROTOCOL_VERSION, 2)
        self.assertEqual(proto.HEADER_LEN, 17)
        self.assertEqual(proto.MSG_RAW_FRAME, 0x01)
        self.assertEqual(proto.MSG_PROCESSED_FRAME, 0x02)
        self.assertEqual(proto.MSG_KEYFRAME_REQ, 0x03)
        self.assertEqual(proto.MSG_PROCESSED_H264, 0x04)

    def test_golden_header(self):
        # encode(type=4, cam=7, capture_ms=1234567, payload) — CONTRACT vector.
        payload = b"\xff\xd8\xff\x00abc"
        env = proto.encode(proto.MSG_PROCESSED_H264, 7, 1234567, payload)
        expected_header = bytes.fromhex("04" "0000000000000007" "000000000012d687")
        self.assertEqual(env[:17], expected_header)
        self.assertEqual(env[17:], payload)

    def test_round_trip(self):
        payload = b"\x00\x00\x01\x65hello"
        env = proto.encode(proto.MSG_PROCESSED_H264, 42, 9001, payload)
        frame = proto.decode(env)
        self.assertEqual(frame.msg_type, proto.MSG_PROCESSED_H264)
        self.assertEqual(frame.camera_id, 42)
        self.assertEqual(frame.capture_ms, 9001)
        self.assertEqual(frame.payload, payload)
        self.assertAlmostEqual(frame.capture_ts, 9.001, places=6)

    def test_decode_too_short(self):
        with self.assertRaises(ValueError):
            proto.decode(b"\x04short")


class H264HelperTest(unittest.TestCase):
    SPS = b"\x00\x00\x00\x01\x67\x42\x00\x1e"
    PPS = b"\x00\x00\x01\x68\xce\x3c\x80"
    IDR = b"\x00\x00\x01\x65\x88\x84"
    PSLICE = b"\x00\x00\x01\x41\x9a\x00"

    def test_looks_like_annexb(self):
        self.assertTrue(h264.looks_like_annexb(self.PSLICE))
        self.assertTrue(h264.looks_like_annexb(self.SPS))
        self.assertFalse(h264.looks_like_annexb(b"\xff\xd8\xffjpeg"))

    def test_keyframe_detection(self):
        self.assertTrue(h264.is_keyframe(self.SPS + self.PPS + self.IDR))
        self.assertTrue(h264.is_keyframe(self.IDR))
        self.assertFalse(h264.is_keyframe(self.PSLICE))

    def test_nal_iteration(self):
        au = self.SPS + self.PPS + self.IDR
        types = [n[0] & 0x1F for n in h264.iter_nal_units(au)]
        self.assertEqual(types, [7, 8, 5])


class H264HubLaneTest(unittest.IsolatedAsyncioTestCase):
    KF = b"\x00\x00\x01\x65idr-data"
    D1 = b"\x00\x00\x01\x41delta-1"
    D2 = b"\x00\x00\x01\x41delta-2"

    async def test_subscribe_starts_at_buffered_keyframe(self):
        hub = hub_mod.FrameHub(1)
        hub.publish_h264(self.KF, True)
        hub.publish_h264(self.D1, False)
        got = [au async for au in hub.subscribe_h264(idle_timeout=0.15)]
        self.assertEqual(got, [self.KF, self.D1])

    async def test_subscribe_skips_leading_deltas_until_keyframe(self):
        hub = hub_mod.FrameHub(1)
        hub.publish_h264(self.D1, False)  # delta before any keyframe

        async def producer():
            await asyncio.sleep(0.02)
            hub.publish_h264(self.KF, True)
            hub.publish_h264(self.D2, False)

        asyncio.create_task(producer())
        got = [au async for au in hub.subscribe_h264(idle_timeout=0.15)]
        self.assertEqual(got, [self.KF, self.D2])

    async def test_has_h264_flag(self):
        hub = hub_mod.FrameHub(1)
        self.assertFalse(hub.has_h264())
        hub.publish_h264(self.KF, True)
        self.assertTrue(hub.has_h264())

    async def test_live_subscribe_drops_backlog_to_latest_keyframe(self):
        hub = hub_mod.FrameHub(1)
        hub.publish_h264(self.KF, True)
        gen = hub.subscribe_h264(idle_timeout=0.05, live_drop=True, max_backlog=2)
        self.assertEqual(await asyncio.wait_for(anext(gen), timeout=0.05), self.KF)

        kf2 = b"\x00\x00\x01\x65idr-2"
        d3 = b"\x00\x00\x01\x41delta-3"
        d4 = b"\x00\x00\x01\x41delta-4"
        hub.publish_h264(self.D1, False)
        hub.publish_h264(self.D2, False)
        hub.publish_h264(kf2, True)
        hub.publish_h264(d3, False)
        hub.publish_h264(d4, False)

        got = [
            await asyncio.wait_for(anext(gen), timeout=0.05),
            await asyncio.wait_for(anext(gen), timeout=0.05),
            await asyncio.wait_for(anext(gen), timeout=0.05),
        ]
        await gen.aclose()
        self.assertEqual(got, [kf2, d3, d4])


class H264LaneSourceTest(unittest.IsolatedAsyncioTestCase):
    """Exactly one producer (edge or worker) may own the H.264 lane at a time."""

    KF_EDGE = b"\x00\x00\x01\x65edge-idr"
    D_EDGE = b"\x00\x00\x01\x41edge-delta"
    KF_WORKER = b"\x00\x00\x01\x65worker-idr"

    async def test_edge_locks_out_worker_while_fresh(self):
        hub = hub_mod.FrameHub(1)
        hub.publish_h264(self.KF_EDGE, True, source="edge")
        hub.publish_h264(self.KF_WORKER, True, source="worker")  # must be dropped
        got = [au async for au in hub.subscribe_h264(idle_timeout=0.1)]
        self.assertEqual(got, [self.KF_EDGE])
        self.assertEqual(hub.stats()["h264_source"], "edge")

    async def test_worker_takes_over_when_edge_stale(self):
        hub = hub_mod.FrameHub(1)
        hub.publish_h264(self.KF_EDGE, True, source="edge")
        hub._h264.received_at -= 30.0  # simulate the edge stream going stale
        hub.publish_h264(self.KF_WORKER, True, source="worker")
        got = [au async for au in hub.subscribe_h264(idle_timeout=0.1)]
        self.assertEqual(got, [self.KF_WORKER])
        self.assertEqual(hub.stats()["h264_source"], "worker")

    async def test_edge_takeover_clears_worker_buffer(self):
        hub = hub_mod.FrameHub(1)
        hub.publish_h264(self.KF_WORKER, True, source="worker")
        hub.publish_h264(self.KF_EDGE, True, source="edge")
        hub.publish_h264(self.D_EDGE, False, source="edge")
        got = [au async for au in hub.subscribe_h264(idle_timeout=0.1)]
        # No worker AU may leak into the edge stream a new subscriber sees.
        self.assertEqual(got, [self.KF_EDGE, self.D_EDGE])

    async def test_default_source_is_worker(self):
        hub = hub_mod.FrameHub(1)
        hub.publish_h264(self.KF_WORKER, True)
        self.assertEqual(hub.stats()["h264_source"], "worker")


class DetectionsStoreTest(unittest.TestCase):
    """Latest worker detection metadata for the client-side overlay."""

    def test_set_and_get(self):
        hub = hub_mod.FrameHub(1)
        self.assertIsNone(hub.latest_detections())
        boxes = [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "label": "person", "conf": 0.9}]
        hub.set_detections(["person"], boxes, 1234)
        det = hub.latest_detections()
        self.assertEqual(det["classes"], ["person"])
        self.assertEqual(det["boxes"], boxes)
        self.assertEqual(det["ts"], 1234)
        self.assertGreater(det["received_at"], 0)


if __name__ == "__main__":
    unittest.main()
