"""board/rsp-leds: the LED ring shows where speech is coming from.

The tool is run as a program against a scripted event stream; what it would send to the ring
is captured in a plain file given as --device and decoded frame by frame.
"""
from __future__ import annotations

import http.server
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from conftest import BOARD_DIR, kill_group

N = 12
FRAME = 4 + 4 * N + 8          # start frame, one word per LED, end frame


def decode(raw: bytes) -> list[list[tuple[int, int, int]]]:
    """Frames written so far, each as 12 (r, g, b) values scaled by the LED's brightness field."""
    frames = []
    for i in range(0, len(raw) - FRAME + 1, FRAME):
        f = raw[i : i + FRAME]
        assert f[:4] == b"\x00" * 4, "bad start frame"
        leds = []
        for n in range(N):
            w = f[4 + 4 * n : 8 + 4 * n]
            assert w[0] & 0xE0 == 0xE0, "LED word must start with 111"
            lit = 1 if (w[0] & 0x1F) else 0
            leds.append((w[3] * lit, w[2] * lit, w[1] * lit))        # wire order is B, G, R
        frames.append(leds)
    return frames


def level(led: tuple[int, int, int]) -> int:
    return sum(led)


class Ring:
    """Runs rsp-leds against a scripted stream. script: list of (event, data) or a float pause."""

    def __init__(self, tmp_path: Path, script: list, *extra: str, hold: float = 5.0, env: dict | None = None):
        self.device = tmp_path / "ring.bin"
        self.device.write_bytes(b"")
        self.served = threading.Event()
        release = self.release = threading.Event()
        served = self.served
        need_token = (env or {}).get("STT_HTTP_TOKEN", "")

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def do_GET(self) -> None:
                if need_token and self.headers.get("Authorization") != "Bearer " + need_token:
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for step in script:
                    if isinstance(step, (int, float)):
                        time.sleep(step)
                        continue
                    kind, data = step
                    self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                served.set()
                release.wait(timeout=hold)          # then the stream ends

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        port = self.srv.server_address[1]
        where = ["--url", f"http://127.0.0.1:{port}/events"] if env is None else []
        self.proc = subprocess.Popen(
            [sys.executable, str(BOARD_DIR / "rsp-leds"), *where, "--device", str(self.device), *extra],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
            env=None if env is None else {"PATH": "/usr/bin:/bin", **{k: v.replace("PORT", str(port)) for k, v in env.items()}},
        )

    def frames(self) -> list[list[tuple[int, int, int]]]:
        return decode(self.device.read_bytes())

    def wait_for(self, cond, timeout: float = 5.0) -> list[tuple[int, int, int]]:
        """Last frame once cond(last frame) holds."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            fr = self.frames()
            if fr:
                last = fr[-1]
                if cond(last):
                    return last
            if self.proc.poll() is not None:
                break
            time.sleep(0.02)
        err = self.proc.stderr.read().decode() if self.proc.poll() is not None else "(still running)"
        raise AssertionError(f"ring never reached the expected state; last frame {last}; stderr {err}")

    def close(self) -> None:
        self.release.set()
        kill_group(self.proc)
        self.srv.shutdown()


def lit(frame) -> set[int]:
    return {i for i, led in enumerate(frame) if level(led) > 0}


def brightest(frame) -> int:
    return max(range(N), key=lambda i: level(frame[i]))


def test_speech_from_240_degrees_lights_the_led_between_mic3_and_mic4(tmp_path: Path) -> None:
    """Measured on the board: LED 0 is at Mic 2 (330 degrees) and LED 3 at 240 degrees."""
    ring = Ring(tmp_path, [("speech_start", {"az": 240, "el": 30, "src": 1})])
    try:
        frame = ring.wait_for(lambda f: lit(f))
        assert brightest(frame) == 3
        assert lit(frame) <= {2, 3, 4}, "only the LED at the talker and its neighbours"
        assert level(frame[3]) > level(frame[2]) > 0 and level(frame[3]) > level(frame[4]) > 0
    finally:
        ring.close()


import pytest


@pytest.mark.parametrize("az,led", [(330, 0), (320, 0), (300, 1), (270, 2), (180, 5), (90, 8), (30, 10), (0, 11), (359, 11), (350, 11)])
def test_led_follows_the_azimuth_clockwise_round_the_ring(tmp_path: Path, az: int, led: int) -> None:
    ring = Ring(tmp_path, [("speech_start", {"az": az})])
    try:
        assert brightest(ring.wait_for(lambda f: lit(f))) == led
    finally:
        ring.close()


def test_ring_goes_dark_when_the_sound_turns_out_not_to_be_an_utterance(tmp_path: Path) -> None:
    ring = Ring(tmp_path, [("speech_start", {"az": 90}), 0.3, ("speech_end", {"az": 90, "sent": False})])
    try:
        ring.wait_for(lambda f: lit(f))
        ring.wait_for(lambda f: not lit(f))
    finally:
        ring.close()


def test_ring_stays_dark_until_the_direction_is_known_then_points(tmp_path: Path) -> None:
    """The tracker usually finds the talker a moment after the level rises; noise it never finds."""
    ring = Ring(tmp_path, [("speech_start", {}), 0.4, ("speech_dir", {"az": 240, "el": 30, "src": 1})])
    try:
        frame = ring.wait_for(lambda f: lit(f))
        assert brightest(frame) == 3 and lit(frame) <= {2, 3, 4}
        assert all(len(lit(f)) <= 3 for f in ring.frames()), "the whole ring was lit before the direction was known"
    finally:
        ring.close()


def test_pointer_follows_a_talker_who_moves_while_speaking(tmp_path: Path) -> None:
    ring = Ring(tmp_path, [("speech_start", {"az": 240}), 0.3, ("speech_dir", {"az": 90})])
    try:
        ring.wait_for(lambda f: lit(f) and brightest(f) == 3)
        moved = ring.wait_for(lambda f: lit(f) and brightest(f) == 8)
        assert lit(moved) <= {7, 8, 9}
    finally:
        ring.close()


def test_utterance_sent_without_a_direction_lights_the_whole_ring_dimly(tmp_path: Path) -> None:
    """Without ODAS (plain microphone mode) there is never a direction to point at."""
    ring = Ring(tmp_path, [("speech_start", {}), ("speech_end", {"sent": True})])
    try:
        frame = ring.wait_for(lambda f: lit(f))
        assert lit(frame) == set(range(N))
        assert len({level(led) for led in frame}) == 1, "no direction, so no LED stands out"
    finally:
        ring.close()


def colour(frame) -> tuple[int, int, int]:
    return frame[brightest(frame)]


def test_while_transcribing_the_pointer_changes_colour_then_confirms_and_goes_dark(tmp_path: Path) -> None:
    ring = Ring(
        tmp_path,
        [("speech_start", {"az": 180}), 0.3, ("speech_end", {"az": 210, "sent": True}), 0.4,
         ("utterance", {"az": 210, "text": "hello"})],
        "--wait", "5", "--flash", "0.3",
    )
    try:
        listening = ring.wait_for(lambda f: lit(f))
        assert brightest(listening) == 5
        working = ring.wait_for(lambda f: lit(f) and colour(f) != colour(listening))
        assert brightest(working) == 4, "the end-of-speech direction is the better estimate"
        done = ring.wait_for(lambda f: lit(f) and colour(f) not in (colour(listening), colour(working)))
        r, g, b = colour(done)
        assert g > r and g > b, "confirmation is green"
        assert brightest(done) == 4
        ring.wait_for(lambda f: not lit(f), timeout=3)
    finally:
        ring.close()


def test_goes_dark_if_no_transcript_follows(tmp_path: Path) -> None:
    """The speech gate or noise filter can drop an utterance after speech_end said it was sent."""
    ring = Ring(tmp_path, [("speech_start", {"az": 60}), ("speech_end", {"az": 60, "sent": True})], "--wait", "0.4")
    try:
        ring.wait_for(lambda f: lit(f))
        started = time.time()
        ring.wait_for(lambda f: not lit(f), timeout=3)
        assert time.time() - started < 2.5
    finally:
        ring.close()


def test_new_speech_during_the_wait_takes_over(tmp_path: Path) -> None:
    ring = Ring(
        tmp_path,
        [("speech_start", {"az": 60}), ("speech_end", {"az": 60, "sent": True}), 0.2, ("speech_start", {"az": 240})],
        "--wait", "0.6",
    )
    try:
        ring.wait_for(lambda f: lit(f) and brightest(f) == 3)
        time.sleep(1.0)                      # the first utterance's wait has long expired
        assert brightest(ring.frames()[-1]) == 3 and lit(ring.frames()[-1]), "still showing the current talker"
    finally:
        ring.close()


def test_dark_when_the_stream_is_lost_and_working_again_after_reconnect(tmp_path: Path) -> None:
    ring = Ring(tmp_path, [("speech_start", {"az": 0})], "--retry", "0.2", hold=0.5)
    try:
        ring.wait_for(lambda f: lit(f) and brightest(f) == 11)
        ring.wait_for(lambda f: not lit(f), timeout=4)                       # stream ended
        ring.wait_for(lambda f: lit(f) and brightest(f) == 11, timeout=4)    # reconnected, script replayed
    finally:
        ring.close()


def test_sigterm_switches_the_ring_off_and_exits_cleanly(tmp_path: Path) -> None:
    ring = Ring(tmp_path, [("speech_start", {"az": 120})])
    try:
        ring.wait_for(lambda f: lit(f))
        ring.proc.send_signal(signal.SIGTERM)
        assert ring.proc.wait(timeout=5) == 0
        assert not lit(ring.frames()[-1])
    finally:
        ring.close()


def test_finds_the_event_stream_from_the_speech_service_settings(tmp_path: Path) -> None:
    """Started by systemd with /etc/default/rsp-stt as its environment, it needs no arguments."""
    ring = Ring(tmp_path, [("speech_start", {"az": 330})],
                env={"STT_HTTP": "127.0.0.1:PORT", "STT_HTTP_TOKEN": "test-ring-token"})
    try:
        assert brightest(ring.wait_for(lambda f: lit(f))) == 0
    finally:
        ring.close()
