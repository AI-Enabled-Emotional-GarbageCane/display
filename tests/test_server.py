from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import DisplayStateStore, create_handler


class DisplayBridgeHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DisplayStateStore(cooldown_sec=60)
        handler = create_handler(ROOT, self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_simulate_accept_updates_state_and_counts(self) -> None:
        snapshot = self.post_json("/api/simulate", {"result": "accept"})

        self.assertEqual(snapshot["current"]["result"], "accept")
        self.assertEqual(snapshot["counts"]["accept"], 1)
        self.assertEqual(snapshot["counts"]["reject"], 0)
        self.assertEqual(snapshot["events"][0]["result"], "accept")

        state = self.get_json("/api/state")
        self.assertEqual(state["current"]["result"], "accept")
        self.assertEqual(state["events"][0]["source"], "simulate")

    def test_low_confidence_does_not_increment_accept_or_reject_counts(self) -> None:
        snapshot = self.post_json("/api/simulate", {"result": "low"})

        self.assertEqual(snapshot["current"]["result"], "low")
        self.assertEqual(snapshot["counts"]["accept"], 0)
        self.assertEqual(snapshot["counts"]["reject"], 0)
        self.assertEqual(snapshot["events"][0]["result"], "low")

    def test_full_payload_with_multiple_objects_is_reject_counted_multi(self) -> None:
        snapshot = self.post_json(
            "/api/simulate",
            {
                "event": "recognition_result",
                "class": "accept",
                "confidence": 0.93,
                "num_objects": 2,
                "snapshot_path": "fixtures/l515-two-objects.jpg",
                "ts": "2026-06-06T21:00:00",
            },
        )

        self.assertEqual(snapshot["current"]["result"], "multi")
        self.assertEqual(snapshot["counts"]["accept"], 0)
        self.assertEqual(snapshot["counts"]["reject"], 1)

    def test_admin_route_serves_separate_admin_page(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/admin", timeout=2) as response:
            html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("Display Admin", html)
        self.assertIn('href="/"', html)

    def get_json(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 202)
            return json.loads(response.read().decode("utf-8"))


class DisplayStaleTextTests(unittest.TestCase):
    def test_display_sources_do_not_contain_stale_contract_terms(self) -> None:
        paths = [
            ROOT / "README.md",
            ROOT / "index.html",
            ROOT / "admin.html",
            ROOT / "app.js",
            ROOT / "server.py",
            ROOT / "styles.css",
        ]
        stale_terms = [
            "D" + "435",
            "YOLOv" + "8",
            "YOLOv" + "8n",
            "user" + "_action",
            "互動 " + "Option 按鈕",
            "turtle-" + "stage",
            "turtle-" + "shell",
            "虛擬" + "海龜",
        ]

        for path in paths:
            text = path.read_text(encoding="utf-8")
            for term in stale_terms:
                with self.subTest(path=path.name, term=term):
                    self.assertNotIn(term, text)

    def test_camera_ui_and_result_glow_are_present(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        styles = (ROOT / "styles.css").read_text(encoding="utf-8")
        script = (ROOT / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="camera-video"', html)
        self.assertIn('id="camera-toggle"', html)
        self.assertIn("assets/sea-turtle-display.png", html)
        self.assertIn('id="impact-image"', html)
        self.assertIn("assets/sea-turtle-accept.png", script)
        self.assertIn("assets/sea-turtle-reject.png", script)
        self.assertIn("navigator.mediaDevices.getUserMedia", script)
        self.assertIn("body.state-accept .camera-frame", styles)
        self.assertIn("body.state-reject .camera-frame", styles)
        self.assertIn("body.state-low .camera-frame", styles)


if __name__ == "__main__":
    unittest.main()
