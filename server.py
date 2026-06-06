from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty
from typing import Any
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT
REQUIRED_RECOGNITION_FIELDS = {"event", "class", "confidence", "num_objects", "snapshot_path", "ts"}
CLASS_VALUES = {"accept", "reject"}


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


def run_queue_consumer(q_result: Any, store: DisplayStateStore, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            payload = q_result.get(timeout=0.25)
        except Empty:
            continue
        try:
            store.process_recognition_result(payload, source="queue")
        except ValueError as exc:
            print(f"[display] ignored invalid recognition_result: {exc}", file=sys.stderr)


def create_handler(static_root: Path, store: DisplayStateStore) -> type[BaseHTTPRequestHandler]:
    root = static_root.resolve()

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
) -> None:
    state_store = store or DisplayStateStore()
    stop_event = threading.Event()
    consumer_thread: threading.Thread | None = None
    if q_result is not None:
        consumer_thread = threading.Thread(target=run_queue_consumer, args=(q_result, state_store, stop_event), daemon=True)
        consumer_thread.start()

    handler = create_handler(static_root, state_store)
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
    args = parser.parse_args()
    print(f"Display bridge listening on http://{args.host}:{args.port}")
    run_display_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
