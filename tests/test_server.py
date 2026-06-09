from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from urllib.parse import quote
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server
from server import DEFAULT_AGX_AUDIO_DEVICE, CameraFrameStore, DisplayStateStore, HostRejectAudioPlayer, create_handler


class FakeHostAudioPlayer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def play_random_reject(self) -> dict:
        self.calls.append("reject")
        return {"played": True, "path": "assets/audio/reject/reject-01.wav", "player": "fake"}


class HostRejectAudioPlayerTests(unittest.TestCase):
    def test_default_agx_hdmi_sink_uses_paplay_device_flag(self) -> None:
        original_which = server.shutil.which
        server.shutil.which = lambda executable: f"/usr/bin/{executable}" if executable == "paplay" else None
        try:
            player = HostRejectAudioPlayer(ROOT)
            command = player._play_command(ROOT / "assets/audio/reject/reject-01.wav")
        finally:
            server.shutil.which = original_which

        self.assertEqual(
            command,
            [
                "/usr/bin/paplay",
                "--device",
                DEFAULT_AGX_AUDIO_DEVICE,
                str(ROOT / "assets/audio/reject/reject-01.wav"),
            ],
        )

    def test_configured_agx_alsa_device_uses_aplay_device_flag(self) -> None:
        original_which = server.shutil.which
        server.shutil.which = lambda executable: f"/usr/bin/{executable}" if executable == "aplay" else None
        try:
            player = HostRejectAudioPlayer(ROOT, audio_device="plughw:1,0")
            command = player._play_command(ROOT / "assets/audio/reject/reject-01.wav")
        finally:
            server.shutil.which = original_which

        self.assertEqual(
            command,
            [
                "/usr/bin/aplay",
                "-q",
                "-D",
                "plughw:1,0",
                str(ROOT / "assets/audio/reject/reject-01.wav"),
            ],
        )


class DisplayBridgeHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DisplayStateStore(cooldown_sec=60)
        self.audio_player = FakeHostAudioPlayer()
        self.camera_frames = CameraFrameStore()
        handler = create_handler(ROOT, self.store, self.audio_player, camera_frames=self.camera_frames)
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
        self.assertEqual(self.audio_player.calls, [])

    def test_simulate_reject_plays_host_audio(self) -> None:
        snapshot = self.post_json("/api/simulate", {"result": "reject"})

        self.assertEqual(snapshot["current"]["result"], "reject")
        self.assertEqual(snapshot["counts"]["accept"], 0)
        self.assertEqual(snapshot["counts"]["reject"], 1)
        self.assertEqual(self.audio_player.calls, ["reject"])
        self.assertEqual(snapshot["audio"]["played"], True)
        self.assertEqual(snapshot["audio"]["player"], "fake")

    def test_audio_disabled_handler_does_not_play_reject(self) -> None:
        store = DisplayStateStore(cooldown_sec=60)
        audio_player = FakeHostAudioPlayer()
        handler = create_handler(ROOT, store, audio_player, audio_enabled=False)
        test_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=test_server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{test_server.server_address[1]}"
            request = urllib.request.Request(
                f"{base_url}/api/simulate",
                data=json.dumps({"result": "reject"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                snapshot = json.loads(response.read().decode("utf-8"))
            self.assertEqual(snapshot["current"]["result"], "reject")
            self.assertNotIn("audio", snapshot)
            self.assertEqual(audio_player.calls, [])
        finally:
            test_server.shutdown()
            test_server.server_close()
            thread.join(timeout=2)

    def test_low_confidence_does_not_increment_accept_or_reject_counts(self) -> None:
        snapshot = self.post_json("/api/simulate", {"result": "low"})

        self.assertEqual(snapshot["current"]["result"], "low")
        self.assertEqual(snapshot["counts"]["accept"], 0)
        self.assertEqual(snapshot["counts"]["reject"], 0)
        self.assertEqual(snapshot["events"][0]["result"], "low")
        self.assertEqual(self.audio_player.calls, [])

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
        self.assertEqual(self.audio_player.calls, [])

    def test_admin_route_serves_separate_admin_page(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/admin", timeout=2) as response:
            html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("Display Admin", html)
        self.assertIn('href="/"', html)

    def test_reject_audio_manifest_lists_only_reject_wavs(self) -> None:
        manifest = self.get_json("/api/reject-audio")
        audio_paths = manifest["reject_audio"]

        self.assertEqual(len(audio_paths), 30)
        self.assertEqual(audio_paths[0], "assets/audio/reject/reject-01.wav")
        self.assertEqual(audio_paths[-1], "assets/audio/reject/reject-30.wav")
        self.assertTrue(all(path.startswith("assets/audio/reject/reject-") for path in audio_paths))
        self.assertTrue(all(path.endswith(".wav") for path in audio_paths))

    def test_snapshot_endpoint_serves_allowed_image(self) -> None:
        path = quote("assets/sea-turtle-display.png")
        with urllib.request.urlopen(f"{self.base_url}/api/snapshot?path={path}", timeout=2) as response:
            payload = response.read()

        self.assertEqual(response.status, 200)
        self.assertGreater(len(payload), 100)
        self.assertEqual(response.headers.get_content_type(), "image/png")

    def test_camera_mjpeg_endpoint_streams_latest_frame(self) -> None:
        self.camera_frames.update_jpeg(b"\xff\xd8fake-jpeg\xff\xd9")

        with urllib.request.urlopen(f"{self.base_url}/api/camera.mjpg", timeout=2) as response:
            payload = response.read(64)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "multipart/x-mixed-replace")
        self.assertIn(b"--frame", payload)
        self.assertIn(b"image/jpeg", payload)

    def test_vision_preview_updates_current_without_counts_events_or_audio(self) -> None:
        snapshot = self.store.process_vision_preview(
            {
                "event": "vision_preview",
                "object_present": True,
                "class": "accept",
                "confidence": 0.82,
                "distance_cm": 28.0,
                "stable_count": 2,
                "stable_required": 4,
                "ts": "2026-06-09T10:00:00",
            },
            source="queue",
        )

        self.assertEqual(snapshot["current"]["event"], "vision_preview")
        self.assertEqual(snapshot["current"]["result"], "detect")
        self.assertEqual(snapshot["current"]["class"], "accept")
        self.assertEqual(snapshot["current"]["confidence"], 0.82)
        self.assertEqual(snapshot["counts"]["accept"], 0)
        self.assertEqual(snapshot["counts"]["reject"], 0)
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(self.audio_player.calls, [])

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
        self.assertNotIn("new Audio", script)
        self.assertNotIn("playRejectRoastAudio", script)
        self.assertIn("navigator.mediaDevices.getUserMedia", script)
        self.assertIn("body.state-accept .camera-frame", styles)
        self.assertIn("body.state-reject .camera-frame", styles)
        self.assertIn("body.state-low .camera-frame", styles)


if __name__ == "__main__":
    unittest.main()
