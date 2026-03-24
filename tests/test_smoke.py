from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from base64 import b64decode
from contextlib import closing
from contextlib import suppress
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.main import app
from app.proxy.routes import reset_worker_proxy_status
from app.thumbnail_jobs import reset_thumbnail_job_stats


class _FakeWorkerClient:
    def __init__(self, *, get_payloads: dict[str, dict] | None = None, post_payloads: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.get_payloads = dict(get_payloads or {})
        self.post_payloads = dict(post_payloads or {})

    async def get(self, url: str, headers: dict[str, str] | None = None):
        self.calls.append(("GET", url, None, headers or {}))
        return self._response(self.get_payloads.get(url, self.get_payloads.get("*", {"ok": True})))

    async def post(self, url: str, content: bytes | None = None, headers: dict[str, str] | None = None):
        self.calls.append(("POST", url, content, headers or {}))
        return self._response(self.post_payloads.get(url, self.post_payloads.get("*", {"ok": True})))

    async def aclose(self) -> None:
        return None

    @staticmethod
    def _response(payload: dict) -> object:
        class _Resp:
            status_code = 200
            headers = {"content-type": "application/json"}
            content = json.dumps(payload).encode("utf-8")

        return _Resp()


class WebSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db = settings.database_path
        self._old_recordings = settings.recordings_path
        self._old_worker_base_url = settings.worker_base_url
        self._old_worker_token = settings.worker_token

        root = Path(self._tmpdir.name)
        settings.database_path = str(root / "sentinelcam.db")
        settings.recordings_path = str(root / "recordings")
        settings.worker_base_url = "http://worker.invalid"
        settings.worker_token = "test-token"
        reset_worker_proxy_status()
        reset_thumbnail_job_stats()

        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        with suppress(Exception):
            self.client_cm.__exit__(None, None, None)
        settings.database_path = self._old_db
        settings.recordings_path = self._old_recordings
        settings.worker_base_url = self._old_worker_base_url
        settings.worker_token = self._old_worker_token
        self._tmpdir.cleanup()

    @property
    def db_path(self) -> str:
        return settings.database_path

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _create_user(self, username: str, role: str = "viewer") -> int:
        with closing(self._db()) as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, "hash", role),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def _create_session(self, user_id: int, session_id: str, csrf_token: str, expires_in_seconds: int = 3600) -> None:
        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO sessions (id, user_id, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, time.time() + expires_in_seconds, "127.0.0.1", "test-agent"),
            )
            conn.commit()
        self.client.cookies.set("session", session_id)
        self.client.cookies.set("csrf_token", csrf_token)

    def _login_as(self, username: str, role: str = "viewer", session_id: str = "session", csrf_token: str = "csrf") -> int:
        user_id = self._create_user(username, role=role)
        self._create_session(user_id, session_id, csrf_token)
        return user_id

    def _wait_for(self, predicate, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def _make_image_bytes(self, image_format: str, color: tuple[int, int, int, int] | tuple[int, int, int]) -> bytes:
        buffer = BytesIO()
        mode = "RGBA" if len(color) == 4 else "RGB"
        with Image.new(mode, (2, 2), color) as img:
            if image_format.upper() == "JPEG":
                img = img.convert("RGB")
            img.save(buffer, format=image_format)
        return buffer.getvalue()

    @staticmethod
    def _make_video_bytes(tag: str) -> bytes:
        return b"\x1a\x45\xdf\xa3" + f"sentinelcam-{tag}".encode("utf-8")

    def test_healthz(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_settings_trim_whitespace_on_assignment(self) -> None:
        root = Path(self._tmpdir.name)
        settings.database_path = f"  {root / 'trimmed.db'}  "
        settings.recordings_path = f"  {root / 'trimmed-recordings'}  "
        settings.worker_base_url = "  http://worker.invalid/base  "

        self.assertEqual(settings.database_path, str(root / "trimmed.db"))
        self.assertEqual(settings.recordings_path, str(root / "trimmed-recordings"))
        self.assertEqual(settings.worker_base_url, "http://worker.invalid/base")

    def test_stream_page_hides_admin_worker_controls_for_viewer(self) -> None:
        self._login_as("viewer1", role="viewer", session_id="viewer-session", csrf_token="viewer-csrf")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Reconnect", response.text)
        self.assertIn("Connection", response.text)
        self.assertIn(">Proxy<", response.text)
        self.assertIn("Technical details", response.text)
        self.assertNotIn("Stop worker", response.text)
        self.assertNotIn('data-cmd="n"', response.text)

    def test_gallery_search_and_detail_navigation_preserve_context(self) -> None:
        admin_id = self._login_as(
            "admin-gallery",
            role="admin",
            session_id="admin-gallery-session",
            csrf_token="admin-gallery-csrf",
        )
        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, raw_filename, size_bytes, created_at) "
                "VALUES (?, 'image', 'alpha.jpg', 'alpha.jpg', 'alpha_raw.jpg', 100, ?)",
                (admin_id, 1000.0),
            )
            alpha_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, raw_filename, size_bytes, created_at) "
                "VALUES (?, 'image', 'beta.jpg', 'beta.jpg', 'beta_raw.jpg', 120, ?)",
                (admin_id, 1010.0),
            )
            beta_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, raw_filename, size_bytes, created_at) "
                "VALUES (?, 'image', 'gamma.jpg', 'gamma.jpg', 'gamma_raw.jpg', 140, ?)",
                (admin_id, 1020.0),
            )
            gamma_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()

        data_response = self.client.get("/gallery/data?q=beta&type=image&sort=oldest")
        self.assertEqual(data_response.status_code, 200)
        payload = data_response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["id"], beta_id)
        self.assertEqual(payload["query"]["q"], "beta")

        detail_response = self.client.get(f"/gallery/{beta_id}?page=3&type=image&sort=oldest&q=a&preset=mine")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn('href="/gallery?page=3&amp;type=image&amp;sort=oldest&amp;q=a&amp;preset=mine"', detail_response.text)
        self.assertIn(
            f'href="/gallery/{alpha_id}?page=3&amp;type=image&amp;sort=oldest&amp;q=a&amp;preset=mine"',
            detail_response.text,
        )
        self.assertIn(
            f'href="/gallery/{gamma_id}?page=3&amp;type=image&amp;sort=oldest&amp;q=a&amp;preset=mine"',
            detail_response.text,
        )
        self.assertIn("Copy link", detail_response.text)
        self.assertIn("Compare", detail_response.text)
        self.assertIn("Same hour activity", detail_response.text)
        self.assertIn(f"{time.strftime('%Y-%m-%d %H:00', time.localtime(1000.0))} to", detail_response.text)

    def test_video_detail_renders_overlay_raw_compare_controls(self) -> None:
        admin_id = self._login_as(
            "admin-video-gallery",
            role="admin",
            session_id="admin-video-gallery-session",
            csrf_token="admin-video-gallery-csrf",
        )
        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, raw_filename, size_bytes, duration_seconds, created_at) "
                "VALUES (?, 'video', 'clip_overlay.webm', 'clip_overlay.webm', 'clip_raw.webm', 2048, 4.2, ?)",
                (admin_id, 2500.0),
            )
            rec_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()

        response = self.client.get(f"/gallery/{rec_id}?mode=compare")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="mode-overlay"', response.text)
        self.assertIn('id="mode-raw"', response.text)
        self.assertIn('id="mode-compare"', response.text)
        self.assertIn('id="single-video"', response.text)
        self.assertIn('id="overlay-video"', response.text)
        self.assertIn('id="raw-video"', response.text)
        self.assertIn('muted preload="metadata" src="/api/recordings/', response.text)
        self.assertIn("captureVideoState", response.text)
        self.assertIn("prepareCompareVideos", response.text)

    def test_gallery_presets_filter_results(self) -> None:
        owner_id = self._login_as("viewer-gallery", role="viewer", session_id="gallery-session", csrf_token="gallery-csrf")
        other_id = self._create_user("other-gallery", role="viewer")
        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, size_bytes, shared, created_at) "
                "VALUES (?, 'image', 'mine.jpg', 'mine.jpg', 100, 0, ?)",
                (owner_id, 2000.0),
            )
            mine_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, size_bytes, shared, created_at) "
                "VALUES (?, 'image', 'shared.jpg', 'shared.jpg', 100, 1, ?)",
                (other_id, 2001.0),
            )
            shared_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, size_bytes, shared, created_at) "
                "VALUES (?, 'video', 'clip.webm', 'clip.webm', 100, 0, ?)",
                (owner_id, 2002.0),
            )
            video_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()

        mine_response = self.client.get("/gallery/data?preset=mine")
        self.assertEqual(mine_response.status_code, 200)
        mine_items = {item["id"] for item in mine_response.json()["items"]}
        self.assertIn(mine_id, mine_items)
        self.assertIn(video_id, mine_items)
        self.assertNotIn(shared_id, mine_items)

        shared_response = self.client.get("/gallery/data?preset=shared")
        self.assertEqual(shared_response.status_code, 200)
        self.assertEqual({item["id"] for item in shared_response.json()["items"]}, {shared_id})

        videos_response = self.client.get("/gallery/data?preset=videos")
        self.assertEqual(videos_response.status_code, 200)
        self.assertEqual({item["id"] for item in videos_response.json()["items"]}, {video_id})

    def test_gallery_data_warms_visible_thumbnails(self) -> None:
        user_id = self._login_as("viewer-warm", role="viewer", session_id="warm-session", csrf_token="warm-csrf")
        png_bytes = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII=")
        rec_dir = Path(settings.recordings_path) / str(user_id)
        rec_dir.mkdir(parents=True, exist_ok=True)
        source_path = rec_dir / "warm.png"
        source_path.write_bytes(png_bytes)

        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, size_bytes, created_at) "
                "VALUES (?, 'image', 'warm.png', 'warm.png', ?, ?)",
                (user_id, len(png_bytes), 3000.0),
            )
            conn.commit()

        response = self.client.get("/gallery/data")
        self.assertEqual(response.status_code, 200)
        thumb_path = rec_dir / "thumb_warm.jpg"
        self.assertTrue(self._wait_for(thumb_path.exists), "thumbnail warm-up did not produce thumb_warm.jpg")

    def test_proxy_cmd_requires_admin(self) -> None:
        fake_worker = _FakeWorkerClient()
        app.state.worker_http_client = fake_worker

        self._login_as("viewer2", role="viewer", session_id="viewer2-session", csrf_token="viewer2-csrf")
        viewer_response = self.client.post(
            "/api/cmd",
            headers={"X-CSRF-Token": "viewer2-csrf", "Content-Type": "application/json"},
            json={"cmd": "i"},
        )
        self.assertEqual(viewer_response.status_code, 403)
        self.assertEqual(fake_worker.calls, [])

        self.client.cookies.clear()
        self._login_as("admin1", role="admin", session_id="admin-session", csrf_token="admin-csrf")
        admin_response = self.client.post(
            "/api/cmd",
            headers={"X-CSRF-Token": "admin-csrf", "Content-Type": "application/json"},
            json={"cmd": "i"},
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(len(fake_worker.calls), 1)
        method, url, body, headers = fake_worker.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/cmd"))
        self.assertEqual(json.loads((body or b"{}").decode("utf-8")), {"cmd": "i"})
        self.assertEqual(headers.get("Authorization"), "Bearer test-token")

    def test_upload_counts_overlay_and_raw_sizes(self) -> None:
        user_id = self._login_as("viewer3", role="viewer", session_id="upload-session", csrf_token="upload-csrf")

        overlay = self._make_image_bytes("JPEG", (24, 28, 32))
        raw = self._make_image_bytes("PNG", (24, 28, 32, 255))
        response = self.client.post(
            "/api/recordings/upload",
            headers={"X-CSRF-Token": "upload-csrf"},
            files={
                "overlay_file": ("overlay.jpg", overlay, "image/jpeg"),
                "raw_file": ("raw.png", raw, "image/png"),
            },
            data={"type": "image"},
        )
        self.assertEqual(response.status_code, 201)

        with closing(self._db()) as conn:
            row = conn.execute(
                "SELECT user_id, size_bytes, raw_filename, overlay_filename FROM recordings"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], user_id)
        self.assertEqual(row["size_bytes"], len(overlay) + len(raw))
        self.assertTrue((Path(settings.recordings_path) / str(user_id) / row["overlay_filename"]).exists())
        self.assertTrue((Path(settings.recordings_path) / str(user_id) / row["raw_filename"]).exists())

    def test_video_upload_stores_raw_variant_and_serves_it(self) -> None:
        user_id = self._login_as("viewer-video", role="viewer", session_id="video-upload-session", csrf_token="video-upload-csrf")

        overlay = self._make_video_bytes("overlay")
        raw = self._make_video_bytes("raw")
        response = self.client.post(
            "/api/recordings/upload",
            headers={"X-CSRF-Token": "video-upload-csrf"},
            files={
                "overlay_file": ("overlay.webm", overlay, "video/webm"),
                "raw_file": ("raw.webm", raw, "video/webm"),
            },
            data={"type": "video", "duration": "3.50"},
        )
        self.assertEqual(response.status_code, 201)
        recording_id = response.json()["id"]

        with closing(self._db()) as conn:
            row = conn.execute(
                "SELECT user_id, size_bytes, raw_filename, overlay_filename, duration_seconds FROM recordings WHERE id = ?",
                (recording_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], user_id)
        self.assertEqual(row["size_bytes"], len(overlay) + len(raw))
        self.assertAlmostEqual(float(row["duration_seconds"]), 3.5, places=2)
        self.assertTrue(row["raw_filename"])

        raw_response = self.client.get(f"/api/recordings/{recording_id}/file?variant=raw")
        self.assertEqual(raw_response.status_code, 200)
        self.assertEqual(raw_response.headers["content-type"], "video/webm")
        self.assertEqual(raw_response.content, raw)

    def test_thumbnail_generation_handles_alpha_png(self) -> None:
        user_id = self._login_as("viewer-thumb", role="viewer", session_id="thumb-session", csrf_token="thumb-csrf")
        png_bytes = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII=")

        response = self.client.post(
            "/api/recordings/upload",
            headers={"X-CSRF-Token": "thumb-csrf"},
            files={
                "overlay_file": ("overlay.png", png_bytes, "image/png"),
                "raw_file": ("raw.png", png_bytes, "image/png"),
            },
            data={"type": "image"},
        )
        self.assertEqual(response.status_code, 201)
        recording_id = response.json()["id"]

        thumb_response = self.client.get(f"/api/recordings/{recording_id}/thumbnail")
        self.assertEqual(thumb_response.status_code, 200)
        self.assertEqual(thumb_response.headers["content-type"], "image/jpeg")
        self.assertEqual(thumb_response.headers["cache-control"], "private, max-age=86400")

    def test_proxy_state_skips_capability_probe_for_modern_worker_payload(self) -> None:
        fake_worker = _FakeWorkerClient(
            get_payloads={
                "http://worker.invalid/api/state": {
                    "ok": True,
                    "preset": "fast",
                    "webrtc_available": True,
                    "mjpeg_available": True,
                    "stream_backend": "webrtc",
                }
            }
        )
        app.state.worker_http_client = fake_worker

        self._login_as("viewer-proxy", role="viewer", session_id="viewer-proxy-session", csrf_token="viewer-proxy-csrf")
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["webrtc_available"])
        self.assertEqual(payload["stream_backend"], "webrtc")
        self.assertEqual([call[1] for call in fake_worker.calls], ["http://worker.invalid/api/state"])

    def test_admin_password_reset_revokes_sessions_and_clears_lockout(self) -> None:
        self._login_as("admin2", role="admin", session_id="admin2-session", csrf_token="admin2-csrf")
        target_id = self._create_user("viewer4", role="viewer")
        with closing(self._db()) as conn:
            conn.execute(
                "UPDATE users SET failed_login_attempts = 5, locked_until = ? WHERE id = ?",
                (time.time() + 600, target_id),
            )
            conn.execute(
                "INSERT INTO sessions (id, user_id, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
                ("victim-session", target_id, time.time() + 3600, "127.0.0.1", "victim-agent"),
            )
            conn.commit()

        response = self.client.patch(
            f"/api/admin/users/{target_id}",
            headers={"X-CSRF-Token": "admin2-csrf"},
            json={"password": "new-very-strong-password"},
        )
        self.assertEqual(response.status_code, 200)

        with closing(self._db()) as conn:
            user_row = conn.execute(
                "SELECT failed_login_attempts, locked_until FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
            session_count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                (target_id,),
            ).fetchone()[0]
        self.assertEqual(user_row["failed_login_attempts"], 0)
        self.assertIsNone(user_row["locked_until"])
        self.assertEqual(session_count, 0)

    def test_admin_ops_endpoint_reports_worker_and_thumbnail_state(self) -> None:
        admin_id = self._login_as("ops-admin", role="admin", session_id="ops-session", csrf_token="ops-csrf")
        png_bytes = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII=")
        rec_dir = Path(settings.recordings_path) / str(admin_id)
        rec_dir.mkdir(parents=True, exist_ok=True)
        (rec_dir / "ops.png").write_bytes(png_bytes)

        with closing(self._db()) as conn:
            conn.execute(
                "INSERT INTO recordings (user_id, type, filename, overlay_filename, size_bytes, created_at) "
                "VALUES (?, 'image', 'ops.png', 'ops.png', ?, ?)",
                (admin_id, len(png_bytes), 5000.0),
            )
            conn.commit()

        self.client.get("/gallery/data")
        thumb_path = rec_dir / "thumb_ops.jpg"
        self.assertTrue(self._wait_for(thumb_path.exists), "ops thumbnail warm-up did not finish in time")

        fake_worker = _FakeWorkerClient()
        app.state.worker_http_client = fake_worker
        state_response = self.client.get("/api/state")
        self.assertEqual(state_response.status_code, 200)

        ops_response = self.client.get("/api/admin/ops")
        self.assertEqual(ops_response.status_code, 200)
        payload = ops_response.json()
        self.assertGreaterEqual(payload["thumbnail"]["completed_count"], 1)
        self.assertEqual(payload["thumbnail"]["pending_count"], 0)
        self.assertIsNotNone(payload["worker"]["last_ok_at"])
        self.assertEqual(payload["worker"]["last_path"], "/api/state")


if __name__ == "__main__":
    unittest.main()
