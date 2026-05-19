"""Import-level smoke tests.

The full HTTP-level test suite was tied to the old SQLite + worker-proxy
architecture; after the move to PostgreSQL + MinIO + Caddy + WireGuard those
tests need rebuilding against ephemeral testcontainers. Until that lands the
checks below at least catch broken imports and obvious wiring problems.
"""
from __future__ import annotations

import importlib
import unittest


class ModuleImportTests(unittest.TestCase):
    def test_core_modules_importable(self) -> None:
        for module_name in (
            "app.config",
            "app.database",
            "app.storage",
            "app.security",
            "app.thumbnail_jobs",
            "app.auth.routes",
            "app.auth.dependencies",
            "app.auth.webauthn",
            "app.dashboard.routes",
            "app.gallery.routes",
            "app.recording.routes",
            "app.main",
        ):
            importlib.import_module(module_name)

    def test_app_router_paths_registered(self) -> None:
        main = importlib.import_module("app.main")
        paths = {route.path for route in getattr(main.app, "routes", [])}
        self.assertIn("/", paths)
        self.assertIn("/healthz", paths)
        self.assertIn("/auth/login", paths)
        self.assertIn("/gallery", paths)
        self.assertIn("/api/recordings/upload", paths)
        self.assertIn("/admin", paths)

    def test_no_legacy_proxy_router(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("app.proxy.routes")


if __name__ == "__main__":
    unittest.main()
