from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import random
import shutil
import sys
import threading
import subprocess
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT
DEFAULT_AUDIO_DEVICE_ENV = "DISPLAY_AUDIO_DEVICE"
DEFAULT_AGX_AUDIO_DEVICE = "alsa_output.platform-3510000.hda.hdmi-stereo"
REQUIRED_RECOGNITION_FIELDS = {"event", "class", "confidence", "num_objects", "snapshot_path", "ts"}
CLASS_VALUES = {"accept", "reject"}
SNAPSHOT_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MJPEG_BOUNDARY = b"frame"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _mock_recognition_result(result: str) -> dict[str, Any]:
    ts = _now_iso()
    samples = {
        "accept": {
            "class": "accept",
            "confidence": 0.91,
            "num_objects": 1,
            "snapshot_path": "mock/l515-accept.jpg",
        },
        "reject": {
            "class": "reject",
            "confidence": 0.84,
            "num_objects": 1,
            "snapshot_path": "mock/l515-reject.jpg",
        },
        "multi": {
            "class": "reject",
            "confidence": 0.76,
            "num_objects": 2,
            "snapshot_path": "mock/l515-multi.jpg",
        },
        "low": {
            "class": "accept",
            "confidence": 0.43,
            "num_objects": 1,
            "snapshot_path": "mock/l515-low-confidence.jpg",
        },
    }
    if result not in samples:
        raise ValueError(f"unknown simulated result: {result}")
    return {"event": "recognition_result", "ts": ts, **samples[result]}


def reject_audio_paths(static_root: Path) -> list[str]:
    return [path.relative_to(static_root).as_posix() for path in reject_audio_files(static_root)]


def reject_audio_files(static_root: Path) -> list[Path]:
    audio_root = static_root / "assets" / "audio" / "reject"
    if not audio_root.is_dir():
        return []
    return [wav_path for wav_path in sorted(audio_root.glob("*.wav")) if wav_path.is_file()]


def _snapshot_roots(static_root: Path) -> tuple[Path, ...]:
    workspace_root = static_root.parent
    return (
        static_root.resolve(),
        (workspace_root / "vision" / "snapshots").resolve(),
    )


def resolve_snapshot_path(static_root: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("snapshot path is required")

    path = Path(raw_path)
    candidate = path.resolve() if path.is_absolute() else (static_root / path).resolve()
    if candidate.suffix.lower() not in SNAPSHOT_SUFFIXES:
        raise ValueError("snapshot must be an image file")
    if not candidate.is_file():
        raise FileNotFoundError(f"snapshot not found: {raw_path}")
    if not any(_is_within_root(candidate, allowed_root) for allowed_root in _snapshot_roots(static_root)):
        raise ValueError("snapshot path is outside allowed roots")
    return candidate


class CameraFrameStore:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._jpeg: bytes | None = None
        self._version = 0

    def update_jpeg(self, jpeg_bytes: bytes) -> None:
        if not jpeg_bytes:
            return
        with self._condition:
            self._jpeg = bytes(jpeg_bytes)
            self._version += 1
            self._condition.notify_all()

    def update_rgb(self, image_rgb: Any, *, quality: int = 82) -> None:
        from PIL import Image

        buffer = io.BytesIO()
        Image.fromarray(image_rgb.astype("uint8", copy=False), mode="RGB").save(buffer, format="JPEG", quality=quality)
        self.update_jpeg(buffer.getvalue())

    def wait_for_frame(self, last_version: int | None = None, *, timeout_sec: float = 10.0) -> tuple[int, bytes] | None:
        with self._condition:
            changed = self._condition.wait_for(
                lambda: self._jpeg is not None and (last_version is None or self._version != last_version),
                timeout=timeout_sec,
            )
            if not changed or self._jpeg is None:
                return None
            return self._version, self._jpeg


class HostRejectAudioPlayer:
    def __init__(self, static_root: Path, *, audio_device: str | None = None) -> None:
        self.static_root = static_root
        self.audio_device = _resolve_audio_device(audio_device)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def play_random_reject(self) -> dict[str, Any]:
        candidates = reject_audio_files(self.static_root)
        if not candidates:
            return {"played": False, "reason": "no_reject_audio_files"}

        audio_path = random.choice(candidates)
        command = self._play_command(audio_path)
        if command is None:
            reason = "no_configured_audio_player" if self.audio_device else "no_host_audio_player"
            return {
                "played": False,
                "path": audio_path.relative_to(self.static_root).as_posix(),
                "reason": reason,
                "audio_device": self.audio_device,
            }

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return {
            "played": True,
            "path": audio_path.relative_to(self.static_root).as_posix(),
            "player": Path(command[0]).name,
            "audio_device": self.audio_device,
        }

    def _play_command(self, audio_path: Path) -> list[str] | None:
        if self.audio_device:
            if _looks_like_pulse_sink(self.audio_device):
                players = (
                    ("paplay", ["--device", self.audio_device, str(audio_path)]),
                    ("aplay", ["-q", "-D", self.audio_device, str(audio_path)]),
                )
            else:
                players = (
                    ("aplay", ["-q", "-D", self.audio_device, str(audio_path)]),
                    ("paplay", ["--device", self.audio_device, str(audio_path)]),
                )
            for executable, args in players:
                player_path = shutil.which(executable)
                if player_path:
                    return [player_path, *args]
            return None

        players = (
            ("paplay", [str(audio_path)]),
            ("aplay", ["-q", str(audio_path)]),
            ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "error", str(audio_path)]),
            ("play", ["-q", str(audio_path)]),
        )
        for executable, args in players:
            player_path = shutil.which(executable)
            if player_path:
                return [player_path, *args]
        return None


def _resolve_audio_device(audio_device: str | None) -> str | None:
    if audio_device is not None:
        return audio_device.strip() or None
    if DEFAULT_AUDIO_DEVICE_ENV in os.environ:
        return os.environ[DEFAULT_AUDIO_DEVICE_ENV].strip() or None
    return DEFAULT_AGX_AUDIO_DEVICE


def _looks_like_pulse_sink(audio_device: str) -> bool:
    return audio_device.startswith(("alsa_output.", "bluez_output.", "auto_null"))


def validate_recognition_result(payload: dict[str, Any]) -> dict[str, Any]:
    missing = REQUIRED_RECOGNITION_FIELDS.difference(payload)
    if missing:
        raise ValueError(f"recognition_result missing fields: {', '.join(sorted(missing))}")
    if payload.get("event") != "recognition_result":
        raise ValueError("event must be recognition_result")
    if payload.get("class") not in CLASS_VALUES:
        raise ValueError("class must be accept or reject")

    confidence = float(payload["confidence"])
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")

    num_objects = int(payload["num_objects"])
    if num_objects < 0:
        raise ValueError("num_objects must be >= 0")

    snapshot_path = str(payload["snapshot_path"])
    if not snapshot_path:
        raise ValueError("snapshot_path is required")

    ts = str(payload["ts"])
    if not ts:
        raise ValueError("ts is required")

    return {
        "event": "recognition_result",
        "class": str(payload["class"]),
        "confidence": confidence,
        "num_objects": num_objects,
        "snapshot_path": snapshot_path,
        "ts": ts,
    }


def validate_vision_preview(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("event") != "vision_preview":
        raise ValueError("event must be vision_preview")

    raw_class = payload.get("class")
    predicted_class = None if raw_class in {None, ""} else str(raw_class)
    if predicted_class is not None and predicted_class not in CLASS_VALUES:
        raise ValueError("preview class must be accept, reject, or empty")

    raw_confidence = payload.get("confidence")
    confidence = None if raw_confidence in {None, ""} else float(raw_confidence)
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("preview confidence must be between 0 and 1")

    raw_distance = payload.get("distance_cm")
    distance_cm = None if raw_distance in {None, ""} else float(raw_distance)
    stable_count = max(0, int(payload.get("stable_count") or 0))
    stable_required = max(1, int(payload.get("stable_required") or 1))
    ts = str(payload.get("ts") or _now_iso())

    return {
        "event": "vision_preview",
        "object_present": bool(payload.get("object_present")),
        "class": predicted_class,
        "confidence": confidence,
        "distance_cm": distance_cm,
        "stable_count": stable_count,
        "stable_required": stable_required,
        "ts": ts,
    }


@dataclass(frozen=True)
class DisplayOutcome:
    result: str
    title: str
    pill: str
    copy: str
    roast: str


OUTCOMES = {
    "idle": DisplayOutcome(
        result="idle",
        pill="待機中",
        title="等待投入",
        copy="全程零按鈕，靠近即偵測，投入後自動判定。",
        roast="靠近我，讓我看看你丟了什麼。",
    ),
    "cooldown": DisplayOutcome(
        result="cooldown",
        pill="冷卻中",
        title="事件已記錄",
        copy="語音與事件紀錄已完成，等待下一次靠近觸發。",
        roast="下一位，請一次丟一樣就好。",
    ),
    "accept": DisplayOutcome(
        result="accept",
        pill="Accept",
        title="分類正確",
        copy="物件已被判定為可接受投入，事件已寫入本機 log。",
        roast="......算你會。",
    ),
    "reject": DisplayOutcome(
        result="reject",
        pill="Reject",
        title="分類錯誤",
        copy="判定結果與本垃圾桶接受類別不符，播放中度 roast 語音。",
        roast="這不是一般垃圾，你最好再看一次。",
    ),
    "multi": DisplayOutcome(
        result="multi",
        pill="Multi-object",
        title="一次丟太多",
        copy="num_objects 大於 1，依保留規則顯示多物件 reject。",
        roast="一次丟一堆是在趕時間？分一下好嗎。",
    ),
    "low": DisplayOutcome(
        result="low",
        pill="Low confidence",
        title="低信心",
        copy="confidence 低於閾值，改播自嘲語音且不計入 accept/reject 統計。",
        roast="我看不太出來欸，可能是我老花。",
    ),
}


class DisplayStateStore:
    def __init__(self, *, confidence_threshold: float = 0.5, cooldown_sec: float = 4.0, max_events: int = 100) -> None:
        self.confidence_threshold = confidence_threshold
        self.cooldown_sec = cooldown_sec
        self.max_events = max_events
        self._condition = threading.Condition()
        self._events: list[dict[str, Any]] = []
        self._accept_count = 0
        self._reject_count = 0
        self._version = 0
        self._current = self._current_payload("idle")
        self._generation = 0
        self._cooldown_timer: threading.Timer | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return self._snapshot_locked()

    def process_recognition_result(self, payload: dict[str, Any], *, source: str = "queue") -> dict[str, Any]:
        result = validate_recognition_result(payload)
        outcome = self._derive_outcome(result)
        event = {
            "time": _time_from_ts(result["ts"]),
            "result": outcome.result,
            "class": result["class"],
            "confidence": result["confidence"],
            "num_objects": result["num_objects"],
            "snapshot_path": result["snapshot_path"],
            "ts": result["ts"],
            "source": source,
            "roast": outcome.roast,
        }

        with self._condition:
            self._generation += 1
            generation = self._generation
            self._cancel_timer_locked()
            if outcome.result == "accept":
                self._accept_count += 1
            elif outcome.result in {"reject", "multi"}:
                self._reject_count += 1

            self._events = [event, *self._events][: self.max_events]
            self._current = self._current_payload(outcome.result, recognition=result, source=source)
            self._publish_locked()

        self._schedule_cooldown(generation)
        return self.snapshot()

    def process_vision_preview(self, payload: dict[str, Any], *, source: str = "queue") -> dict[str, Any]:
        preview = validate_vision_preview(payload)
        with self._condition:
            self._generation += 1
            self._cancel_timer_locked()
            self._current = self._preview_current_payload(preview, source=source)
            self._publish_locked()
        return self.snapshot()

    def wait_for_update(self, last_version: int, timeout_sec: float = 15.0) -> dict[str, Any] | None:
        with self._condition:
            changed = self._condition.wait_for(lambda: self._version != last_version, timeout=timeout_sec)
            if not changed:
                return None
            return self._snapshot_locked()

    def _derive_outcome(self, payload: dict[str, Any]) -> DisplayOutcome:
        if payload["confidence"] < self.confidence_threshold:
            return OUTCOMES["low"]
        if payload["num_objects"] > 1:
            return OUTCOMES["multi"]
        return OUTCOMES["accept" if payload["class"] == "accept" else "reject"]

    def _preview_current_payload(self, preview: dict[str, Any], *, source: str) -> dict[str, Any]:
        if not preview["object_present"]:
            payload = self._current_payload("idle", source=source)
            payload.update({"event": "vision_preview", "object_present": False})
            return payload

        label = preview.get("class")
        confidence = preview.get("confidence")
        stable_count = int(preview.get("stable_count") or 0)
        stable_required = int(preview.get("stable_required") or 1)
        if confidence is None:
            result = "detect"
            title = "即時監看中"
            copy = "L515 已看到物體，等待模型輸出。"
        elif confidence < self.confidence_threshold:
            result = "low"
            title = "低信心"
            copy = "模型信心不足，請讓物體更清楚。"
        else:
            result = "detect"
            title = "即時辨識中"
            label_text = "Accept" if label == "accept" else "Reject"
            copy = f"目前傾向 {label_text}，穩定 {stable_count}/{stable_required} 後才會記錄與播放。"

        return {
            "event": "vision_preview",
            "result": result,
            "pill": "Realtime",
            "title": title,
            "copy": copy,
            "roast": "",
            "confidence": confidence,
            "class": label,
            "num_objects": 1,
            "snapshot_path": None,
            "distance_cm": preview.get("distance_cm"),
            "object_present": True,
            "stable_count": stable_count,
            "stable_required": stable_required,
            "ts": preview["ts"],
            "source": source,
        }

    def _current_payload(
        self,
        result: str,
        *,
        recognition: dict[str, Any] | None = None,
        source: str = "system",
    ) -> dict[str, Any]:
        outcome = OUTCOMES[result]
        return {
            "result": outcome.result,
            "pill": outcome.pill,
            "title": outcome.title,
            "copy": outcome.copy,
            "roast": outcome.roast,
            "confidence": None if recognition is None else recognition["confidence"],
            "class": None if recognition is None else recognition["class"],
            "num_objects": None if recognition is None else recognition["num_objects"],
            "snapshot_path": None if recognition is None else recognition["snapshot_path"],
            "ts": _now_iso() if recognition is None else recognition["ts"],
            "source": source,
        }

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "version": self._version,
            "threshold": self.confidence_threshold,
            "current": dict(self._current),
            "counts": {
                "accept": self._accept_count,
                "reject": self._reject_count,
                "accuracy_rate": _accuracy_rate(self._accept_count, self._reject_count),
            },
            "events": [dict(event) for event in self._events],
        }

    def _schedule_cooldown(self, generation: int) -> None:
        timer = threading.Timer(self.cooldown_sec, self._enter_cooldown, args=(generation,))
        timer.daemon = True
        with self._condition:
            if generation != self._generation:
                return
            self._cooldown_timer = timer
        timer.start()

    def _enter_cooldown(self, generation: int) -> None:
        with self._condition:
            if generation != self._generation:
                return
            self._current = self._current_payload("cooldown")
            self._publish_locked()

        timer = threading.Timer(max(0.8, self.cooldown_sec / 2), self._enter_idle, args=(generation,))
        timer.daemon = True
        with self._condition:
            if generation != self._generation:
                return
            self._cooldown_timer = timer
        timer.start()

    def _enter_idle(self, generation: int) -> None:
        with self._condition:
            if generation != self._generation:
                return
            self._current = self._current_payload("idle")
            self._cooldown_timer = None
            self._publish_locked()

    def _cancel_timer_locked(self) -> None:
        if self._cooldown_timer is not None:
            self._cooldown_timer.cancel()
            self._cooldown_timer = None

    def _publish_locked(self) -> None:
        self._version += 1
        self._condition.notify_all()


def _time_from_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except ValueError:
        return _now_time()


def _accuracy_rate(accept_count: int, reject_count: int) -> int | None:
    total = accept_count + reject_count
    if total == 0:
        return None
    return round((accept_count / total) * 100)


def _play_audio_for_reject(snapshot: dict[str, Any], audio_player: HostRejectAudioPlayer | None) -> dict[str, Any] | None:
    if audio_player is None:
        return None
    if snapshot.get("current", {}).get("result") != "reject":
        return None
    return audio_player.play_random_reject()


def run_queue_consumer(
    q_result: Any,
    store: DisplayStateStore,
    stop_event: threading.Event,
    audio_player: HostRejectAudioPlayer | None = None,
) -> None:
    while not stop_event.is_set():
        try:
            payload = q_result.get(timeout=0.25)
        except Empty:
            continue
        try:
            if payload.get("event") == "vision_preview":
                store.process_vision_preview(payload, source="queue")
            else:
                snapshot = store.process_recognition_result(payload, source="queue")
                _play_audio_for_reject(snapshot, audio_player)
        except ValueError as exc:
            print(f"[display] ignored invalid vision payload: {exc}", file=sys.stderr)


def create_handler(
    static_root: Path,
    store: DisplayStateStore,
    audio_player: HostRejectAudioPlayer | None = None,
    *,
    audio_enabled: bool = True,
    camera_frames: CameraFrameStore | None = None,
) -> type[BaseHTTPRequestHandler]:
    root = static_root.resolve()
    host_audio_player = None if not audio_enabled else audio_player if audio_player is not None else HostRejectAudioPlayer(root)

    class DisplayRequestHandler(BaseHTTPRequestHandler):
        server_version = "DisplayBridge/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/events":
                self._send_events()
                return
            if parsed.path == "/api/state":
                self._send_json(store.snapshot())
                return
            if parsed.path == "/api/reject-audio":
                self._send_json({"reject_audio": reject_audio_paths(root)})
                return
            if parsed.path == "/api/snapshot":
                self._send_snapshot(parsed.query)
                return
            if parsed.path == "/api/camera.mjpg":
                self._send_camera_stream()
                return
            self._send_static(parsed.path)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/simulate":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            try:
                body = self._read_json_body()
                payload = body if body.get("event") == "recognition_result" else _mock_recognition_result(str(body.get("result", "")))
                snapshot = store.process_recognition_result(payload, source="simulate")
                audio_status = _play_audio_for_reject(snapshot, host_audio_player)
                if audio_status is not None:
                    snapshot = {**snapshot, "audio": audio_status}
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._send_json(snapshot, status=HTTPStatus.ACCEPTED)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            snapshot = store.snapshot()
            version = int(snapshot["version"])
            if not self._write_sse(snapshot):
                return

            while True:
                next_snapshot = store.wait_for_update(version)
                if next_snapshot is None:
                    if not self._write_raw(b": ping\n\n"):
                        return
                    continue
                version = int(next_snapshot["version"])
                if not self._write_sse(next_snapshot):
                    return

        def _send_static(self, request_path: str) -> None:
            if request_path in {"", "/"}:
                path = "/index.html"
            elif request_path == "/admin":
                path = "/admin.html"
            else:
                path = request_path
            candidate = (root / unquote(path.lstrip("/"))).resolve()
            if not _is_within_root(candidate, root) or not candidate.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            self._send_file(candidate)

        def _send_snapshot(self, query: str) -> None:
            try:
                params = parse_qs(query)
                raw_path = params.get("path", [""])[0]
                candidate = resolve_snapshot_path(root, raw_path)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return

            self._send_file(candidate)

        def _send_camera_stream(self) -> None:
            if camera_frames is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "camera stream is not attached")
                return

            first_frame = camera_frames.wait_for_frame(timeout_sec=15.0)
            if first_frame is None:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "camera frame is not ready")
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY.decode('ascii')}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            version, jpeg = first_frame
            if not self._write_mjpeg_frame(jpeg):
                return
            while True:
                frame = camera_frames.wait_for_frame(version, timeout_sec=10.0)
                if frame is None:
                    continue
                version, jpeg = frame
                if not self._write_mjpeg_frame(jpeg):
                    return

        def _write_mjpeg_frame(self, jpeg: bytes) -> bool:
            header = (
                b"--" + MJPEG_BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
            )
            return self._write_raw(header + jpeg + b"\r\n")

        def _send_file(self, candidate: Path) -> None:
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            payload = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _write_sse(self, payload: dict[str, Any]) -> bool:
            data = json.dumps(payload, ensure_ascii=False)
            return self._write_raw(f"event: state\ndata: {data}\n\n".encode("utf-8"))

        def _write_raw(self, payload: bytes) -> bool:
            try:
                self.wfile.write(payload)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return False
            return True

    return DisplayRequestHandler


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def run_display_server(
    q_result: Any | None = None,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    static_root: Path = STATIC_ROOT,
    store: DisplayStateStore | None = None,
    camera_frames: CameraFrameStore | None = None,
    audio_device: str | None = None,
    audio_enabled: bool = True,
) -> None:
    state_store = store or DisplayStateStore()
    audio_player = (
        HostRejectAudioPlayer(static_root.resolve(), audio_device=audio_device)
        if audio_enabled
        else None
    )
    stop_event = threading.Event()
    consumer_thread: threading.Thread | None = None
    if q_result is not None:
        consumer_thread = threading.Thread(target=run_queue_consumer, args=(q_result, state_store, stop_event, audio_player), daemon=True)
        consumer_thread.start()

    handler = create_handler(static_root, state_store, audio_player, audio_enabled=audio_enabled, camera_frames=camera_frames)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
        if consumer_thread is not None:
            consumer_thread.join(timeout=1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve AI 情緒垃圾筒 Display UI and local SSE bridge.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host. Use 0.0.0.0 for same-LAN browsers.")
    parser.add_argument("--port", default=8080, type=int, help="HTTP port.")
    parser.add_argument(
        "--audio-device",
        default=None,
        help=(
            "Audio output device for AGX-connected speakers. "
            "Examples: alsa_output.platform-3510000.hda.hdmi-stereo, plughw:0,3, or hw:0,3. "
            f"Can also be set with {DEFAULT_AUDIO_DEVICE_ENV}. "
            f"Default: {DEFAULT_AGX_AUDIO_DEVICE}."
        ),
    )
    parser.add_argument("--no-audio", action="store_true", help="Disable display-side host audio playback.")
    args = parser.parse_args()
    print(f"Display bridge listening on http://{args.host}:{args.port}")
    if args.no_audio:
        print("Display audio disabled")
    else:
        print(f"Display audio device: {_resolve_audio_device(args.audio_device) or 'system default'}")
    run_display_server(host=args.host, port=args.port, audio_device=args.audio_device, audio_enabled=not args.no_audio)


if __name__ == "__main__":
    main()
