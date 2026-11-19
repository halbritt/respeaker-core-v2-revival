"""F3: Board Runtime (`board/`) host-isolated unit and localhost E2E tests."""
from __future__ import annotations

import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np
import pytest

from conftest import BOARD_DIR, free_ports, make_bounded_sleep_stub, make_fake_ort, run_bounded


def _pcm16_frame(amp: int, samples: int = 480) -> bytes:
    """Create one 16-bit mono frame (default 30 ms at 16 kHz) with alternating +/- amplitude."""
    arr = np.full(samples, amp, dtype="<i2")
    arr[1::2] = -amp
    return arr.tobytes()


# --- Tier 1: Happy-Path Board Runtime (8 tests) ---


def test_f3_t1_audio_primitives_mono_frame_rms16_and_wav_bytes(rsp_stt_loader) -> None:
    stt = rsp_stt_loader(env={"STT_CHANNEL": "sum", "STT_CHANNELS": "8", "STT_MICS": "6"})
    frame = np.zeros((480, 8), dtype="<i2")
    for c in range(6):
        frame[:, c] = 600
    frame[:, 2] = 1200
    # Channels 6-7 are playback loopback and must be ignored by MIX="sum"
    frame[:, 6] = 30000
    frame[:, 7] = 30000
    raw = frame.tobytes()

    mono_sum = stt.mono_frame(raw)
    assert stt.rms16(mono_sum) == 700

    stt_ch2 = rsp_stt_loader(env={"STT_CHANNEL": "2", "STT_CHANNELS": "8", "STT_MICS": "6"})
    mono_ch2 = stt_ch2.mono_frame(raw)
    assert stt_ch2.rms16(mono_ch2) == 1200

    wav_data = stt.wav_bytes(mono_sum)
    with wave.open(io.BytesIO(wav_data), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 480)


def test_f3_t1_segmenter_detects_speech_emits_start_end_and_submits(rsp_stt_loader) -> None:
    stt = rsp_stt_loader(
        env={
            "STT_MIN_RMS": "150",
            "STT_START_RATIO": "3.0",
            "STT_PREROLL_MS": "90",
            "STT_MIN_UTTERANCE_MS": "120",
            "STT_END_SILENCE_MS": "90",
            "STT_MIN_DYN_DB": "0",
        }
    )
    events: queue.Queue = queue.Queue()
    stt.SUBSCRIBERS.append(events)

    meta_seq = iter([{"az": 0, "el": 15, "src": 1}, {"az": 350, "el": 10, "src": 1}, {"az": 10, "el": 20, "src": 1}])
    last_meta = {"az": 10, "el": 20, "src": 1}

    def next_meta():
        nonlocal last_meta
        last_meta = next(meta_seq, last_meta)
        return last_meta

    seg = stt.Segmenter(meta=next_meta, name="test-seg")
    quiet = _pcm16_frame(10)
    loud_hi = _pcm16_frame(1200)
    loud_lo = _pcm16_frame(600)

    for _ in range(4):
        seg.feed(quiet)
    for idx in range(24):
        seg.feed(loud_hi if idx % 2 == 0 else loud_lo)
    for _ in range(4):
        seg.feed(quiet)

    seen_events = []
    while not events.empty():
        seen_events.append(events.get_nowait())
    kinds = [e["event"] for e in seen_events]
    assert "speech_start" in kinds
    end_ev = [e for e in seen_events if e["event"] == "speech_end"][0]
    assert end_ev["sent"] is True

    utt, meta = stt.PENDING.get_nowait()
    assert len(utt) >= 10
    assert meta["az"] == 0
    assert meta["el"] == 15
    assert meta["src"] == 1
    assert "t" in meta and "dyn_db" in meta


def test_f3_t1_has_speech_with_mocked_onnxruntime_detects_and_short_circuits(rsp_stt_loader, tmp_path: Path) -> None:
    fake_ort = make_fake_ort([0.9, 0.9, 0.9, 0.9, 0.9])
    stt = rsp_stt_loader(
        env={"STT_SPEECH_MIN_MS": "64", "STT_SPEECH_MODEL": str(tmp_path / "silero.onnx")},
        fake_ort=fake_ort,
    )
    pcm = _pcm16_frame(1000, samples=512 * 5)
    assert stt.has_speech(pcm) is True
    # need = 64 // 32 = 2 consecutive chunks; loop must short-circuit after 2 calls
    assert len(fake_ort.run_calls) == 2


def test_f3_t1_transcribe_and_emit_post_whisper_and_log_jsonl(
    rsp_stt_loader, whisper_mock_factory, tmp_path: Path
) -> None:
    base_url, captured = whisper_mock_factory("Computer, turn on the light")
    log_path = tmp_path / "transcripts.jsonl"
    stt = rsp_stt_loader(
        env={
            "STT_SERVER": f"{base_url}/inference",
            "STT_WEBHOOK": f"{base_url}/webhook",
            "STT_LANGUAGE": "en",
            "STT_LOG": str(log_path),
        }
    )

    text = stt.transcribe(_pcm16_frame(800))
    assert text == "Computer, turn on the light"
    assert captured[0]["path"] == "/inference"
    assert b'name="file"; filename="u.wav"' in captured[0]["body"]
    assert b"RIFF" in captured[0]["body"]

    stt.emit(text, 1.25, 0.08, {"az": 90, "t": 3.4})
    logged = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert logged["text"] == "Computer, turn on the light"
    assert logged["az"] == 90
    assert len(stt.HISTORY) == 1
    assert captured[1]["path"] == "/webhook"
    assert json.loads(captured[1]["body"].decode("utf-8"))["text"] == "Computer, turn on the light"


def test_f3_t1_event_server_sse_recent_and_bearer_auth(rsp_stt_loader, event_server_factory) -> None:
    stt = rsp_stt_loader(env={"STT_HTTP_TOKEN": "test-token"})
    srv, port = event_server_factory(stt)

    received: list[str] = []
    ready = threading.Event()

    def sse_reader() -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/events",
            headers={"Authorization": "Bearer test-token", "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ready.set()
            for raw in resp:
                line = raw.decode("utf-8").rstrip("\r\n")
                received.append(line)
                if "hello sse" in line:
                    break

    t = threading.Thread(target=sse_reader, daemon=True)
    t.start()
    assert ready.wait(timeout=3)
    deadline = time.time() + 2.0
    while not stt.SUBSCRIBERS and time.time() < deadline:
        time.sleep(0.01)
    assert len(stt.SUBSCRIBERS) == 1

    stt.publish("speech_start", {"az": 45})
    stt.publish("utterance", {"text": "hello sse", "az": 45})
    t.join(timeout=5)
    assert not t.is_alive()

    joined = "\n".join(received)
    assert "event: speech_start" in joined
    assert "event: utterance" in joined
    assert "hello sse" in joined

    recent_req = urllib.request.Request(
        f"http://127.0.0.1:{port}/recent",
        headers={"Authorization": "Bearer test-token"},
    )
    with urllib.request.urlopen(recent_req, timeout=3) as resp:
        arr = json.loads(resp.read().decode("utf-8"))
    assert len(arr) == 1 and arr[0]["text"] == "hello sse"


def test_f3_t1_track_reader_parses_chunked_odas_json_over_socketpair(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    sock_srv, sock_cli = socket.socketpair()
    t = threading.Thread(target=stt.track_reader, args=(sock_srv,), daemon=True)
    t.start()

    pkt1 = json.dumps(
        {
            "timeStamp": 1,
            "src": [
                {"id": 1, "x": 0.0, "y": 1.0, "z": 0.0, "activity": 0.9},
                {"id": 2, "x": 1.0, "y": 0.0, "z": 0.0, "activity": 0.1},
            ],
        }
    ).encode("utf-8")
    pkt2 = json.dumps(
        {
            "timeStamp": 2,
            "src": [
                {"id": 0, "x": 0.0, "y": 1.0, "z": 0.0, "activity": 0.9},
                {"id": 3, "x": -1.0, "y": 0.0, "z": 1.0, "activity": 0.8},
            ],
        }
    ).encode("utf-8")

    # Split packet 1 across two chunks and concatenate packet 2
    sock_cli.sendall(pkt1[:25])
    time.sleep(0.02)
    sock_cli.sendall(pkt1[25:] + b"\n" + pkt2)
    sock_cli.close()
    t.join(timeout=3)
    sock_srv.close()
    assert not t.is_alive()

    assert stt.TRACKS[0] is None
    assert stt.TRACKS[1] == {"src": 3, "az": 180.0, "el": 45.0}


def test_f3_t1_odas_config_and_capture_to_record_request(rsp_stt_loader, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    rec_dir = tmp_path / "rec"
    run_dir.mkdir()
    rec_dir.mkdir()
    stt = rsp_stt_loader(
        env={
            "STT_RUN_DIR": str(run_dir),
            "STT_REC_DIR": str(rec_dir),
            "STT_ODAS_TRACK_PORT": "9100",
            "STT_ODAS_AUDIO_PORT": "9102",
            "STT_ODAS_SEP_PORT": "9103",
        }
    )

    cfg_path = Path(stt.odas_config(str(run_dir / "raw.fifo")))
    cfg_text = cfg_path.read_text(encoding="utf-8")
    assert f'type = "file"; path = "{run_dir / "raw.fifo"}";' in cfg_text
    assert 'type = "soundcard"' not in cfg_text
    for port in ("9100", "9102", "9103"):
        assert f"port = {port};" in cfg_text

    # Test capture_to in an isolated subprocess because capture_to calls os._exit(1) on EOF
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_arecord = bin_dir / "arecord"
    fake_arecord.write_text("#!/bin/sh\ndd if=/dev/zero bs=15360 count=25 2>/dev/null\n", encoding="utf-8")
    fake_arecord.chmod(0o755)

    fifo_path = run_dir / "raw.fifo"
    os.mkfifo(fifo_path)
    (run_dir / "record.req").write_text("0.15 my/label\n", encoding="utf-8")

    child_code = (
        "import importlib.util, os, threading\n"
        f"spec = importlib.util.spec_from_file_location('rsp_stt_c', {str(BOARD_DIR / 'rsp-stt.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"def drain():\n"
        f"    with open({str(fifo_path)!r}, 'rb') as f:\n"
        f"        while f.read(15360): pass\n"
        "threading.Thread(target=drain, daemon=True).start()\n"
        f"m.capture_to({str(fifo_path)!r})\n"
    )
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", STT_RUN_DIR=str(run_dir), STT_REC_DIR=str(rec_dir))
    res = subprocess.run([sys.executable, "-B", "-c", child_code], env=env, capture_output=True, text=True, timeout=10, check=False)
    assert res.returncode == 1
    assert not (run_dir / "record.active").exists()
    raw_files = list(rec_dir.glob("*-my_label.raw"))
    assert len(raw_files) == 1
    assert raw_files[0].stat().st_size == 5 * 15360


def test_f3_t1_rsp_listen_wake_follow_strip_wake_and_command_env(tmp_path: Path) -> None:
    sse_events = [
        ("utterance", {"text": "background noise only", "az": 10, "ts": "2026-11-17T00:00:01Z"}),
        ("utterance", {"text": "Computer, turn on lights", "az": 45, "ts": "2026-11-17T00:00:02Z"}),
        ("utterance", {"text": "and dim it", "az": 46, "ts": "2026-11-17T00:00:03Z"}),
    ]
    done_serving = threading.Event()

    import http.server

    class SSEHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for kind, payload in sse_events:
                msg = f"event: {kind}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
                self.wfile.write(msg)
                self.wfile.flush()
            done_serving.wait(timeout=5)

    srv = http.server.HTTPServer(("127.0.0.1", 0), SSEHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    hits_file = tmp_path / "hits.jsonl"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "row = {'stdin': json.loads(sys.stdin.read()), 'RSP_TEXT': os.environ['RSP_TEXT'], "
        "'RSP_AZ': os.environ['RSP_AZ'], 'RSP_TS': os.environ['RSP_TS']}\n"
        f"with open({str(hits_file)!r}, 'a') as f:\n"
        "    f.write(json.dumps(row) + '\\n')\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            str(BOARD_DIR / "rsp-listen"),
            "--url",
            f"http://127.0.0.1:{port}/events",
            "--wake",
            "computer",
            "--follow",
            "10",
            "--strip-wake",
            "--",
            sys.executable,
            str(probe),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if hits_file.exists() and len(hits_file.read_text(encoding="utf-8").splitlines()) >= 2:
                break
            time.sleep(0.05)
        if not hits_file.is_file():
            rc = proc.poll()
            if rc is not None:
                out, err = proc.communicate(timeout=2)
                diag = f"rc={rc}, stdout={out!r}, stderr={err!r}"
            else:
                diag = "rsp-listen still running"
            pytest.fail(f"hits_file {hits_file} was not created before deadline ({diag})")
        lines = hits_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first, second = json.loads(lines[0]), json.loads(lines[1])
        assert first["RSP_TEXT"] == "turn on lights"
        assert first["RSP_AZ"] == "45"
        assert first["stdin"]["text"] == "turn on lights"
        assert second["RSP_TEXT"] == "and dim it"
    finally:
        done_serving.set()
        proc.terminate()
        proc.wait(timeout=5)
        srv.shutdown()
        srv.server_close()


# --- Tier 2: Boundary, Corner & Error Paths (8 tests) ---


def test_f3_t2_clean_ignored_and_duplicate_edge_cases(rsp_stt_loader) -> None:
    stt = rsp_stt_loader(env={"STT_ODAS_IGNORE": "350:20"})

    assert stt.clean("[BLANK_AUDIO]") == ""
    assert stt.clean("*sad music*") == ""
    assert stt.clean("thank you.") == ""
    assert stt.clean("...") == ""
    assert stt.clean("*cough* hello") == "*cough* hello"

    assert stt.ignored(5.0) is True
    assert stt.ignored(330.0) is True
    assert stt.ignored(329.9) is False
    assert stt.ignored(11.0) is False

    stt.RECENT.append((10.0, 12.0, "turn on the kitchen light"))
    assert stt.duplicate(10.0, 12.0, "turn on the kitchen light") is True
    assert stt.duplicate(8.5, 12.0, "turn on the kitchen light") is False
    assert stt.duplicate(10.0, 12.0, "completely different words here") is False


def test_f3_t2_segmenter_drops_untracked_ignored_flat_dyn_and_caps_at_soft_or_max(rsp_stt_loader) -> None:
    quiet = _pcm16_frame(10)
    loud_const = _pcm16_frame(1200)
    loud_lo = _pcm16_frame(200)

    # 1) untracked drop
    stt1 = rsp_stt_loader(env={"STT_MIN_RMS": "150", "STT_PREROLL_MS": "60", "STT_END_SILENCE_MS": "60", "STT_MIN_UTTERANCE_MS": "90"})
    seg_untracked = stt1.Segmenter(meta=lambda: None, name="untracked")
    for _ in range(10):
        seg_untracked.feed(loud_const)
    for _ in range(3):
        seg_untracked.feed(quiet)
    assert stt1.PENDING.empty()

    # 2) ignored direction drop (feed >=16 loud frames so len(utt) crosses 10 and self.seen populates)
    ign_env = {
        "STT_MIN_RMS": "150",
        "STT_PREROLL_MS": "60",
        "STT_END_SILENCE_MS": "60",
        "STT_MIN_UTTERANCE_MS": "90",
        "STT_ODAS_IGNORE": "350:20",
        "STT_MIN_DYN_DB": "0",
    }
    stt2 = rsp_stt_loader(env=ign_env)
    q_ign: queue.Queue = queue.Queue()
    stt2.SUBSCRIBERS.append(q_ign)
    seg_ign = stt2.Segmenter(meta=lambda: {"az": 355, "el": 0, "src": 1}, name="ign")
    for _ in range(16):
        seg_ign.feed(loud_const)
    for _ in range(3):
        seg_ign.feed(quiet)
    ign_events = []
    while not q_ign.empty():
        ign_events.append(q_ign.get_nowait())
    end_ign = [e for e in ign_events if e["event"] == "speech_end"][0]
    assert end_ign.get("az") == 355, f"Expected tracked az=355 (not untracked drop), got {end_ign}"
    assert end_ign["sent"] is False
    assert stt2.PENDING.empty()

    # Positive control: exact same frame sequence without STT_ODAS_IGNORE enqueues into PENDING
    stt2_ctrl = rsp_stt_loader(env={**ign_env, "STT_ODAS_IGNORE": ""})
    seg_ign_ctrl = stt2_ctrl.Segmenter(meta=lambda: {"az": 355, "el": 0, "src": 1}, name="ign-ctrl")
    for _ in range(16):
        seg_ign_ctrl.feed(loud_const)
    for _ in range(3):
        seg_ign_ctrl.feed(quiet)
    assert stt2_ctrl.PENDING.qsize() == 1

    # 3) flat dynamic range drop (feed >=16 loud frames so len(utt) crosses 10 and self.seen populates)
    flat_env = {
        "STT_MIN_RMS": "150",
        "STT_PREROLL_MS": "60",
        "STT_END_SILENCE_MS": "60",
        "STT_MIN_UTTERANCE_MS": "90",
        "STT_MIN_DYN_DB": "15",
    }
    stt3 = rsp_stt_loader(env=flat_env)
    q_flat: queue.Queue = queue.Queue()
    stt3.SUBSCRIBERS.append(q_flat)
    seg_flat = stt3.Segmenter(meta=lambda: {"az": 90, "el": 0, "src": 1}, name="flat")
    for _ in range(16):
        seg_flat.feed(loud_const)
    for _ in range(3):
        seg_flat.feed(quiet)
    flat_events = []
    while not q_flat.empty():
        flat_events.append(q_flat.get_nowait())
    end_flat = [e for e in flat_events if e["event"] == "speech_end"][0]
    assert end_flat.get("az") == 90, f"Expected tracked az=90 (not untracked drop), got {end_flat}"
    assert end_flat["dyn_db"] < 15.0
    assert end_flat["sent"] is False
    assert stt3.PENDING.empty()

    # Positive control: exact same frame sequence with STT_MIN_DYN_DB=0 enqueues into PENDING
    stt3_ctrl = rsp_stt_loader(env={**flat_env, "STT_MIN_DYN_DB": "0"})
    seg_flat_ctrl = stt3_ctrl.Segmenter(meta=lambda: {"az": 90, "el": 0, "src": 1}, name="flat-ctrl")
    for _ in range(16):
        seg_flat_ctrl.feed(loud_const)
    for _ in range(3):
        seg_flat_ctrl.feed(quiet)
    assert stt3_ctrl.PENDING.qsize() == 1

    # 4) soft cap & hard max cap
    stt4 = rsp_stt_loader(
        env={
            "STT_MIN_RMS": "150",
            "STT_PREROLL_MS": "60",
            "STT_MIN_UTTERANCE_MS": "90",
            "STT_END_SILENCE_MS": "900",
            "STT_SOFT_UTTERANCE_S": "0.3",
            "STT_SOFT_SILENCE_MS": "60",
            "STT_MAX_UTTERANCE_S": "0.6",
            "STT_MIN_DYN_DB": "0",
        }
    )
    seg_soft = stt4.Segmenter(name="soft")
    for idx in range(12):
        seg_soft.feed(loud_const if idx % 2 == 0 else loud_lo)
    # Only 2 silence frames (60ms) needed once >= 300ms
    for _ in range(2):
        seg_soft.feed(quiet)
    assert seg_soft.speaking is False
    assert stt4.PENDING.qsize() == 1

    seg_max = stt4.Segmenter(name="max")
    for _ in range(25):
        seg_max.feed(loud_const)
    assert seg_max.speaking is False
    assert stt4.PENDING.qsize() == 2


def test_f3_t2_has_speech_graceful_fallback_short_audio_and_reset_run(rsp_stt_loader, tmp_path: Path) -> None:
    # Missing onnxruntime fails open
    stt_missing = rsp_stt_loader(env={"STT_SPEECH_MIN_MS": "200"}, block_ort=True)
    pcm = _pcm16_frame(1000, samples=1024)
    assert stt_missing.has_speech(pcm) is True
    assert stt_missing.GATE == [None]

    # Short audio (<512 samples) and interrupted run return False when gate active
    fake_ort = make_fake_ort([0.9, 0.2, 0.9, 0.2])
    stt_active = rsp_stt_loader(
        env={"STT_SPEECH_MIN_MS": "64", "STT_SPEECH_MODEL": str(tmp_path / "m.onnx")},
        fake_ort=fake_ort,
    )
    assert stt_active.has_speech(_pcm16_frame(1000, samples=510)) is False
    assert stt_active.has_speech(_pcm16_frame(1000, samples=512 * 4)) is False


def test_f3_t2_event_server_auth_rejects_401_404_and_drops_slow_subscriber(
    rsp_stt_loader, event_server_factory
) -> None:
    stt = rsp_stt_loader(env={"STT_HTTP_TOKEN": "test-tok123"})
    _, port = event_server_factory(stt)

    with pytest.raises(urllib.error.HTTPError) as exc_401:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/recent", timeout=3)
    assert exc_401.value.code == 401

    bad_tok = urllib.request.Request(f"http://127.0.0.1:{port}/recent", headers={"Authorization": "Bearer wrong"})
    with pytest.raises(urllib.error.HTTPError) as exc_wrong:
        urllib.request.urlopen(bad_tok, timeout=3)
    assert exc_wrong.value.code == 401

    good_404 = urllib.request.Request(f"http://127.0.0.1:{port}/nope", headers={"Authorization": "Bearer test-tok123"})
    with pytest.raises(urllib.error.HTTPError) as exc_404:
        urllib.request.urlopen(good_404, timeout=3)
    assert exc_404.value.code == 404

    slow_q: queue.Queue = queue.Queue(maxsize=200)
    for idx in range(200):
        slow_q.put_nowait({"i": idx})
    stt.SUBSCRIBERS.append(slow_q)
    stt.publish("speech_start", {"az": 0})
    assert slow_q not in stt.SUBSCRIBERS


def test_f3_t2_odas_config_missing_soundcard_exits_and_emit_handles_io_errors(
    rsp_stt_loader, tmp_path: Path
) -> None:
    bad_cfg = tmp_path / "bad.cfg"
    bad_cfg.write_text('raw: { interface: { type = "file"; path = "/tmp/x"; } }\n', encoding="utf-8")
    unwritable_dir = tmp_path / "dir_not_file"
    unwritable_dir.mkdir()

    stt = rsp_stt_loader(
        env={
            "STT_ODAS_CFG": str(bad_cfg),
            "STT_LOG": str(unwritable_dir),
            "STT_WEBHOOK": "http://127.0.0.1:1/hook",
        }
    )
    with pytest.raises(SystemExit):
        stt.odas_config(str(tmp_path / "raw.fifo"))

    # emit must log warnings and not raise on OSError or webhook failure
    stt.emit("hello", 0.5, 0.1, {"az": 10})
    assert len(stt.HISTORY) == 1


def test_f3_t2_rsp_record_inactive_service_zero_writes_and_sandboxed_cap_and_timeout(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_sys = bin_dir / "systemctl"
    fake_sys.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_sys.chmod(0o755)
    # Polling loops in rsp-record call sleep; the stub keeps them fast and bounded, and
    # every run has a timeout, so a broken guard fails the test instead of spinning.
    make_bounded_sleep_stub(bin_dir)

    # Unmodified rsp-record fails cleanly when service is inactive
    base_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home_unmod")}
    res = run_bounded(["sh", str(BOARD_DIR / "rsp-record"), "1", "probe"], env=base_env, timeout=20)
    assert res.returncode == 1
    assert "not running" in res.stderr

    # Sandboxed copy rewriting only hardcoded /run, $HOME/recordings, and /sys LED paths
    fake_sys.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    rec_home = tmp_path / "home"
    rec_dir = rec_home / "recordings"
    led_dir = tmp_path / "led"
    run_dir.mkdir()
    rec_dir.mkdir(parents=True)
    led_dir.mkdir()
    (led_dir / "trigger").write_text("mmc0\n", encoding="utf-8")
    (led_dir / "brightness").write_text("0\n", encoding="utf-8")

    # Stub sudo to just exec its arguments without privilege escalation
    fake_sudo = bin_dir / "sudo"
    fake_sudo.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    fake_sudo.chmod(0o755)

    script_text = (BOARD_DIR / "rsp-record").read_text(encoding="utf-8")
    sandbox_rec = tmp_path / "rsp-record"
    sandbox_rec.write_text(
        script_text.replace("run=/run/rsp-stt", f"run={run_dir}").replace(
            'led="/sys/class/leds/respeaker2:blue:state2"', f'led="{led_dir}"'
        ),
        encoding="utf-8",
    )
    sandbox_rec.chmod(0o755)

    sb_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(rec_home), "RSP_REC_CAP_MB": "0"}

    # Already recording error
    (run_dir / "record.active").write_text("/tmp/existing.raw\n", encoding="utf-8")
    res_busy = run_bounded(["sh", str(sandbox_rec), "1", "x"], env=sb_env, timeout=20)
    assert res_busy.returncode == 1 and "already recording" in res_busy.stderr
    (run_dir / "record.active").unlink()

    # Cap pruning + timeout when service never creates record.active
    old_raw = rec_dir / "20260101T000000Z-old.raw"
    old_jsonl = rec_dir / "20260101T000000Z-old.transcript.jsonl"
    old_raw.write_bytes(b"\x01" * 1_500_000)
    old_jsonl.write_text('{"text": "old"}\n', encoding="utf-8")
    res_to = run_bounded(["sh", str(sandbox_rec), "2", "captest"], env=sb_env, timeout=20)
    assert res_to.returncode == 1 and "did not start recording" in res_to.stderr
    assert not old_raw.exists() and not old_jsonl.exists()
    assert (run_dir / "record.req").read_text(encoding="utf-8").strip() == "2 captest"

    # Happy path with responder creating out_raw & toggling record.active
    (run_dir / "record.req").unlink(missing_ok=True)
    out_raw = rec_dir / "20261117T000000Z-happy.raw"

    def responder() -> None:
        req_f = run_dir / "record.req"
        act_f = run_dir / "record.active"
        deadline = time.time() + 3.0
        while not req_f.exists() and time.time() < deadline:
            time.sleep(0.002)
        out_raw.write_bytes(b"\x00" * 100)
        act_f.write_text(str(out_raw) + "\n", encoding="utf-8")
        time.sleep(0.05)
        act_f.unlink()

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    res_ok = run_bounded(["sh", str(sandbox_rec), "1", "happy"], env=sb_env, timeout=20)
    t.join(timeout=3)
    assert res_ok.returncode == 0, res_ok.stderr
    assert (led_dir / "trigger").read_text(encoding="utf-8").strip() == "none"
    assert (led_dir / "brightness").read_text(encoding="utf-8").strip() == "0"


def _replay_env_probe(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for rsp-stt.py that records the STT_* environment rsp-replay gives it."""
    stub_stt = tmp_path / "stub_stt.py"
    captured_env = tmp_path / "captured.json"
    stub_stt.write_text(
        "import json, os, sys\n"
        "print('[skip-bracket] noise')\n"
        "print(' skip-space noise')\n"
        "print('keep-this-line')\n"
        f"open({str(captured_env)!r}, 'w').write(json.dumps({{k: v for k, v in os.environ.items() if k.startswith('STT_')}}))\n"
        "open(os.environ['STT_LOG'], 'w').write('{\"text\": \"ok\"}\\n')\n",
        encoding="utf-8",
    )
    return stub_stt, captured_env


def test_f3_t2_rsp_replay_missing_defaults_error_and_env_override_precedence(tmp_path: Path) -> None:
    raw = tmp_path / "take.raw"
    raw.write_bytes(b"\x00" * 64)

    # No <defaults dir>/rsp-stt: the script stops with an error and runs nothing
    empty = tmp_path / "empty-defaults"
    empty.mkdir()
    stub_stt, captured_env = _replay_env_probe(tmp_path)
    res_missing = run_bounded(
        ["sh", str(BOARD_DIR / "rsp-replay"), str(raw)],
        env={"PATH": "/usr/bin:/bin", "RSP_DEFAULTS_DIR": str(empty), "RSP_STT": str(stub_stt)},
        timeout=20,
    )
    assert res_missing.returncode != 0
    assert not captured_env.exists()

    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "rsp-stt").write_text((BOARD_DIR / "rsp-stt.env").read_text(encoding="utf-8"), encoding="utf-8")
    track, audio, sep = free_ports(3)

    custom_odas = tmp_path / "fake-odaslive"
    res = run_bounded(
        ["sh", str(BOARD_DIR / "rsp-replay"), str(raw), str(custom_odas)],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RSP_DEFAULTS_DIR": str(defaults),
            "RSP_REPLAY_TRACK_PORT": str(track),
            "RSP_REPLAY_AUDIO_PORT": str(audio),
            "RSP_REPLAY_SEP_PORT": str(sep),
            "RSP_STT": str(stub_stt),
            "STT_SERVER": "http://127.0.0.1:7777/inference",
        },
        timeout=20,
    )
    assert res.returncode == 0, res.stderr
    assert "skip-bracket" not in res.stdout
    assert "skip-space" not in res.stdout
    assert "keep-this-line" in res.stdout
    assert "wrote " in res.stdout and "(1 transcripts)" in res.stdout

    env_seen = json.loads(captured_env.read_text(encoding="utf-8"))
    assert env_seen["STT_SERVER"] == "http://127.0.0.1:7777/inference"     # environment beats the file
    assert env_seen["STT_LANGUAGE"] == "en"                                # from the defaults file
    assert env_seen["STT_ODAS_BIN"] == str(custom_odas)
    assert env_seen["STT_SOURCE"] == "odas"
    assert env_seen["STT_ODAS_INPUT"] == str(raw.resolve())
    assert (env_seen["STT_ODAS_TRACK_PORT"], env_seen["STT_ODAS_AUDIO_PORT"], env_seen["STT_ODAS_SEP_PORT"]) == (
        str(track),
        str(audio),
        str(sep),
    )
    assert not Path(env_seen["STT_RUN_DIR"]).exists()


def test_f3_t2_rsp_replay_default_ports_stay_clear_of_the_live_service(tmp_path: Path) -> None:
    raw = tmp_path / "take.raw"
    raw.write_bytes(b"\x00" * 64)
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    # even if the site file pins the live service's ports, a replay must not reuse them
    (defaults / "rsp-stt").write_text(
        "STT_SERVER=http://whisper.example:8910/inference\nSTT_ODAS_TRACK_PORT=9000\n", encoding="utf-8"
    )
    stub_stt, captured_env = _replay_env_probe(tmp_path)
    res = run_bounded(
        ["sh", str(BOARD_DIR / "rsp-replay"), str(raw)],
        env={"PATH": "/usr/bin:/bin", "RSP_DEFAULTS_DIR": str(defaults), "RSP_STT": str(stub_stt)},
        timeout=20,
    )
    assert res.returncode == 0, res.stderr
    env_seen = json.loads(captured_env.read_text(encoding="utf-8"))
    assert (env_seen["STT_ODAS_TRACK_PORT"], env_seen["STT_ODAS_AUDIO_PORT"], env_seen["STT_ODAS_SEP_PORT"]) == (
        "9100",
        "9102",
        "9103",
    )


def test_f3_t2_rsp_replay_site_overrides_file_is_read_after_the_defaults(tmp_path: Path) -> None:
    """<defaults dir>/rsp-stt.local wins over rsp-stt; the environment wins over both."""
    raw = tmp_path / "take.raw"
    raw.write_bytes(b"\x00" * 64)
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "rsp-stt").write_text(
        "STT_SERVER=http://base.example:8910/inference\nSTT_LANGUAGE=en\nSTT_END_SILENCE_MS=1500\n",
        encoding="utf-8",
    )
    (defaults / "rsp-stt.local").write_text(
        "STT_SERVER=http://site.example:8910/inference\nSTT_LANGUAGE=de\nSTT_ODAS_IGNORE=308:20\n",
        encoding="utf-8",
    )
    stub_stt, captured_env = _replay_env_probe(tmp_path)
    base_env = {"PATH": "/usr/bin:/bin", "RSP_DEFAULTS_DIR": str(defaults), "RSP_STT": str(stub_stt)}

    res = run_bounded(["sh", str(BOARD_DIR / "rsp-replay"), str(raw)], env=base_env, timeout=20)
    assert res.returncode == 0, res.stderr
    seen = json.loads(captured_env.read_text(encoding="utf-8"))
    assert seen["STT_SERVER"] == "http://site.example:8910/inference"
    assert seen["STT_LANGUAGE"] == "de"
    assert seen["STT_ODAS_IGNORE"] == "308:20"
    assert seen["STT_END_SILENCE_MS"] == "1500"          # only in the defaults file

    res = run_bounded(
        ["sh", str(BOARD_DIR / "rsp-replay"), str(raw)], env={**base_env, "STT_LANGUAGE": "fr"}, timeout=20
    )
    assert res.returncode == 0, res.stderr
    seen = json.loads(captured_env.read_text(encoding="utf-8"))
    assert seen["STT_LANGUAGE"] == "fr"
    assert seen["STT_SERVER"] == "http://site.example:8910/inference"

    # without a site file the defaults apply
    (defaults / "rsp-stt.local").unlink()
    res = run_bounded(["sh", str(BOARD_DIR / "rsp-replay"), str(raw)], env=base_env, timeout=20)
    assert res.returncode == 0, res.stderr
    seen = json.loads(captured_env.read_text(encoding="utf-8"))
    assert seen["STT_SERVER"] == "http://base.example:8910/inference"
    assert "STT_ODAS_IGNORE" not in seen


def test_f3_t2_board_config_and_service_invariants() -> None:
    cfg = (BOARD_DIR / "odas-respeaker-core-v2.cfg").read_text(encoding="utf-8")
    assert 'version = "2.1";' in cfg
    assert "fS = 16000;" in cfg
    assert "nBits = 32;" in cfg
    assert "nChannels = 8;" in cfg
    assert len(re.findall(r"angle\s*=\s*\(\s*180\.0,\s*180\.0\s*\);", cfg)) == 6
    assert "nPots = 4;" in cfg
    for port in ("9000", "9002", "9003"):
        assert f"port = {port};" in cfg

    svc = (BOARD_DIR / "rsp-stt.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/default/rsp-stt" in svc
    assert "ExecStart=/usr/bin/python3 -u /opt/rsp-stt/rsp-stt.py" in svc

    env_txt = (BOARD_DIR / "rsp-stt.env").read_text(encoding="utf-8")
    assert "STT_ODAS_CFG=/opt/rsp-stt/odas-respeaker-core-v2.cfg" in env_txt
    assert "STT_SOURCE=odas" in env_txt


def test_f3_t2_rsp_replay_keeps_environment_overrides_that_contain_spaces(tmp_path: Path) -> None:
    """An STT_* override such as a phrase list has spaces and shell characters in it."""
    raw = tmp_path / "take.raw"
    raw.write_bytes(b"\x00" * 64)
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    (defaults / "rsp-stt").write_text((BOARD_DIR / "rsp-stt.env").read_text(encoding="utf-8"), encoding="utf-8")
    stub_stt, captured_env = _replay_env_probe(tmp_path)
    track, audio, sep = free_ports(3)
    phrases = "thank you.|bye for now.|it's $HOME; \"quoted\""
    res = run_bounded(
        ["sh", str(BOARD_DIR / "rsp-replay"), str(raw)],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RSP_DEFAULTS_DIR": str(defaults),
            "RSP_REPLAY_TRACK_PORT": str(track),
            "RSP_REPLAY_AUDIO_PORT": str(audio),
            "RSP_REPLAY_SEP_PORT": str(sep),
            "RSP_STT": str(stub_stt),
            "STT_NOISE_PHRASES": phrases,
            "STT_LANGUAGE": "de",
        },
        timeout=20,
    )
    assert res.returncode == 0, res.stderr
    env_seen = json.loads(captured_env.read_text(encoding="utf-8"))
    assert env_seen["STT_NOISE_PHRASES"] == phrases
    assert env_seen["STT_LANGUAGE"] == "de"            # still beats the defaults file


def test_the_image_installs_and_enables_the_cpu_frequency_policy() -> None:
    """Armbian's 25% threshold kept the CPU at top voltage; ours must be set after Armbian's."""
    from conftest import BOARD_DIR, NEWOS_DIR
    unit = (BOARD_DIR / "rsp-cpufreq.service").read_text(encoding="utf-8")
    assert "After=armbian-hardware-optimization.service" in unit
    assert "echo 85 > /sys/devices/system/cpu/cpufreq/ondemand/up_threshold" in unit
    script = (NEWOS_DIR / "mk-sd-image.sh").read_text(encoding="utf-8")
    assert "board/rsp-cpufreq.service" in script
    assert "for u in rsp-leds rsp-led-power rsp-cpufreq; do" in script
