"""What board/rsp-stt.py does with a finished utterance, and which ODAS beam it sends.

Each test drives the real code through a public seam and looks at what comes out:
`handle()` (the uploader's per-utterance step) with the speech gate and the Whisper server
replaced by fakes, observed through the JSON-lines log (STT_LOG) and published events; and
the whole program in offline mode against a fake odaslive, observed through the audio that
reaches a mock Whisper server.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from conftest import BOARD_DIR, free_ports, make_fake_ort, run_bounded


def _pcm(seconds: float, amp: int = 1000) -> bytes:
    """Mono 16-bit, 16 kHz square wave of the given length."""
    arr = np.full(int(seconds * 16000), amp, dtype="<i2")
    arr[1::2] = -amp
    return arr.tobytes()


def _logged(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _script_transcripts(monkeypatch: pytest.MonkeyPatch, stt, texts: list[str]) -> list[int]:
    """Make transcribe() return `texts` in turn; the returned list records each call."""
    calls: list[int] = []
    pending = list(texts)

    def fake_transcribe(pcm: bytes) -> str:
        calls.append(len(pcm))
        return pending.pop(0)

    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    return calls


# --- the speech gate ---


def test_utterance_rejected_by_speech_gate_is_neither_transcribed_nor_emitted(
    rsp_stt_loader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate_env = {"STT_SPEECH_MIN_MS": "96", "STT_SPEECH_MODEL": str(tmp_path / "silero.onnx")}

    no_speech = make_fake_ort(0.05)                      # the model hears no speech anywhere
    stt = rsp_stt_loader(env=gate_env, fake_ort=no_speech)
    calls = _script_transcripts(monkeypatch, stt, ["Whisper invents a sentence for noise."])
    stt.handle([_pcm(1.0)], {"t": 5.0, "az": 90})
    assert no_speech.run_calls, "the gate was not consulted"
    assert calls == [], "rejected audio was sent to the server"
    assert _logged(tmp_path / "transcripts.jsonl") == []
    assert list(stt.HISTORY) == []

    # control: the same audio with a model that hears speech is transcribed and emitted
    stt_ok = rsp_stt_loader(env=gate_env, fake_ort=make_fake_ort(0.95))
    calls_ok = _script_transcripts(monkeypatch, stt_ok, ["Open the window."])
    stt_ok.handle([_pcm(1.0)], {"t": 5.0, "az": 90})
    assert len(calls_ok) == 1
    rows = _logged(tmp_path / "transcripts.jsonl")
    assert [r["text"] for r in rows] == ["Open the window."]
    assert rows[0]["az"] == 90 and rows[0]["audio_s"] == 1.0
    assert [h["text"] for h in stt_ok.HISTORY] == ["Open the window."]


# --- noise artefacts ---


def test_noise_only_transcripts_are_dropped_and_real_ones_emitted(
    rsp_stt_loader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stt = rsp_stt_loader(env={"STT_SPEECH_MIN_MS": "0"})
    noise = ["Thank you.", "*music*", " [BLANK_AUDIO] ", "(silence)", "you", "...", "THANK YOU."]
    real = ["Turn on the desk light.", "Thank you. That is all for now.", "*cough* is anyone there?"]
    order = noise[:4] + real[:1] + noise[4:] + real[1:]
    calls = _script_transcripts(monkeypatch, stt, order)
    for n in range(len(order)):
        stt.handle([_pcm(1.0)], {"t": 10.0 * (n + 1)})        # far apart: never duplicates

    assert len(calls) == len(order)
    assert [r["text"] for r in _logged(tmp_path / "transcripts.jsonl")] == real
    assert [h["text"] for h in stt.HISTORY] == real


# --- duplicates from overlapping sources ---


def test_second_transcript_inside_an_emitted_span_with_the_same_words_is_dropped(
    rsp_stt_loader, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stt = rsp_stt_loader(env={"STT_SPEECH_MIN_MS": "0"})
    _script_transcripts(
        monkeypatch,
        stt,
        [
            "turn on the kitchen light please",
            "Turn on the kitchen light",            # inside the first span, same words: a copy
            "what is the weather tomorrow",         # inside the first span, other words: kept
            "turn on the kitchen light please",     # same words, half a minute later: kept
        ],
    )
    stt.handle([_pcm(3.0)], {"t": 10.0, "az": 10})   # covers 7.0 .. 10.0 s
    stt.handle([_pcm(2.0)], {"t": 9.5, "az": 200})   # covers 7.5 .. 9.5 s
    stt.handle([_pcm(2.0)], {"t": 9.5, "az": 200})
    stt.handle([_pcm(3.0)], {"t": 40.0, "az": 10})

    rows = _logged(tmp_path / "transcripts.jsonl")
    assert [(r["text"], r["t"]) for r in rows] == [
        ("turn on the kitchen light please", 10.0),
        ("what is the weather tomorrow", 9.5),
        ("turn on the kitchen light please", 40.0),
    ]


# --- the request sent to the Whisper server ---


def _multipart_fields(content_type: str, body: bytes) -> dict[str, bytes]:
    m = re.search(r"boundary=(\S+)", content_type)
    assert m is not None, content_type
    fields: dict[str, bytes] = {}
    for part in body.split(b"--" + m.group(1).encode()):
        head, sep, value = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if sep and name:
            fields[name.group(1).decode()] = value[:-2] if value.endswith(b"\r\n") else value
    return fields


def test_request_to_whisper_carries_the_configured_language(
    rsp_stt_loader, whisper_mock_factory, tmp_path: Path
) -> None:
    base_url, captured = whisper_mock_factory("Guten Morgen")
    stt = rsp_stt_loader(
        env={"STT_SERVER": f"{base_url}/inference", "STT_LANGUAGE": "de", "STT_SPEECH_MIN_MS": "0"}
    )
    stt.handle([_pcm(0.5)], {"t": 3.0})

    assert len(captured) == 1 and captured[0]["path"] == "/inference"
    fields = _multipart_fields(captured[0]["content_type"], captured[0]["body"])
    assert fields["language"] == b"de"
    assert fields["response_format"] == b"json"
    assert fields["temperature"] == b"0.0"
    with wave.open(io.BytesIO(fields["file"]), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 16000, 8000)
    assert [r["text"] for r in _logged(tmp_path / "transcripts.jsonl")] == ["Guten Morgen"]


# --- dominant-source following (STT_ODAS_MODE=dominant) ---

# The fake odaslive plays a plan: segments of [frames, level of slot 0, level of slot 1] on
# the postfiltered stream (which drives beam selection and the VAD). On the separated
# stream, which is what gets sent, each slot carries its own constant amplitude, so every
# 30 ms frame of the audio that reaches Whisper shows which slot it was taken from.
SLOT0_MARK, SLOT1_MARK = 500, 2000
HYSTERESIS_FRAMES = 8          # 240 ms of 30 ms frames: a rival must lead this long

FAKE_ODASLIVE = r"""
import json, os, re, socket, sys, time
import numpy as np
cfg = open(sys.argv[sys.argv.index('-c') + 1]).read()
tp, sp, ap = [int(p) for p in re.findall(r'port = (\d+);', cfg)][:3]   # tracked, separated, postfiltered
st = socket.create_connection(('127.0.0.1', tp), timeout=5)
ss = socket.create_connection(('127.0.0.1', sp), timeout=5)
sa = socket.create_connection(('127.0.0.1', ap), timeout=5)
st.sendall((json.dumps({'src': [
    {'id': 1, 'x': 1.0, 'y': 0.0, 'z': 0.0, 'activity': 0.9},
    {'id': 2, 'x': 0.0, 'y': 1.0, 'z': 0.0, 'activity': 0.9}]}) + '\n').encode())
time.sleep(0.3)                                    # let the track reader take it in
def frame(l0, l1):
    f = np.zeros((480, 4), dtype='<i2')
    for col, lvl in ((0, l0), (1, l1)):
        f[::2, col] = lvl; f[1::2, col] = -lvl
    return f.tobytes()
for n, pf0, pf1 in json.load(open(os.environ['FAKE_ODAS_PLAN'])):
    quiet = pf0 == 0 and pf1 == 0
    pf = frame(pf0, pf1)
    sep = frame(0, 0) if quiet else frame(%d, %d)
    for _ in range(n):
        ss.sendall(sep); sa.sendall(pf)
sa.close(); ss.close(); st.close()
""" % (SLOT0_MARK, SLOT1_MARK)


def _run_dominant(tmp_path: Path, whisper_mock_factory, plan: list[list[int]]) -> tuple[list[int], list[dict]]:
    """Replay `plan` through rsp-stt.py (offline, dominant mode). Returns the per-frame
    amplitude of the one utterance sent to Whisper, and the logged transcripts."""
    base_url, captured = whisper_mock_factory("someone is talking")
    fake = tmp_path / "fake_odaslive.py"
    fake.write_text(FAKE_ODASLIVE, encoding="utf-8")
    fake_bin = tmp_path / "fake-odaslive"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable!r} {str(fake)!r} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    recording = tmp_path / "take.raw"
    recording.write_bytes(b"")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log_file = tmp_path / "transcripts.jsonl"
    track_port, audio_port, sep_port = free_ports(3)

    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_ODAS_PLAN": str(plan_file),
            "STT_SOURCE": "odas",
            "STT_ODAS_MODE": "dominant",
            "STT_ODAS_SEND": "separated",
            "STT_ODAS_INPUT": str(recording),          # offline: exit when the streams end
            "STT_ODAS_BIN": str(fake_bin),
            "STT_ODAS_CFG": str(BOARD_DIR / "odas-respeaker-core-v2.cfg"),
            "STT_ODAS_TRACK_PORT": str(track_port),
            "STT_ODAS_AUDIO_PORT": str(audio_port),
            "STT_ODAS_SEP_PORT": str(sep_port),
            "STT_SERVER": f"{base_url}/inference",
            "STT_RUN_DIR": str(run_dir),
            "STT_REC_DIR": str(tmp_path / "rec"),
            "STT_LOG": str(log_file),
            "STT_HTTP": "",
            "STT_SPEECH_MIN_MS": "0",
            "STT_PREROLL_MS": "300",
            "STT_END_SILENCE_MS": "150",
            "STT_MIN_UTTERANCE_MS": "150",
            "STT_MIN_DYN_DB": "0",
        }
    )
    res = run_bounded([sys.executable, "-u", str(BOARD_DIR / "rsp-stt.py")], env=env, timeout=60)
    assert res.returncode == 0, f"rsp-stt.py failed:\n{res.stdout}\n{res.stderr}"
    assert len(captured) == 1, f"expected one utterance, got {len(captured)}:\n{res.stdout}\n{res.stderr}"

    fields = _multipart_fields(captured[0]["content_type"], captured[0]["body"])
    with wave.open(io.BytesIO(fields["file"]), "rb") as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    frames = samples[: len(samples) // 480 * 480].reshape(-1, 480)
    return [int(np.abs(f).max()) for f in frames], _logged(log_file)


QUIET = [5, 0, 0]
TAIL = [12, 0, 0]


def test_dominant_mode_louder_slot_takes_over_only_after_the_hysteresis_period(
    tmp_path: Path, whisper_mock_factory
) -> None:
    # slot 0 talks for 10 frames, then slot 1 is three times as loud for 20 frames
    marks, rows = _run_dominant(tmp_path, whisper_mock_factory, [QUIET, [10, 1000, 0], [20, 1000, 3000], TAIL])
    voiced = [m for m in marks if m]
    assert set(voiced) == {SLOT0_MARK, SLOT1_MARK}, marks
    # the beam stays on slot 0 for the first 7 frames of the rival's lead and moves on the 8th
    assert voiced == [SLOT0_MARK] * (10 + HYSTERESIS_FRAMES - 1) + [SLOT1_MARK] * (20 - HYSTERESIS_FRAMES + 1), marks
    assert len(rows) == 1 and rows[0]["text"] == "someone is talking" and "az" in rows[0]


def test_dominant_mode_short_bursts_from_another_slot_do_not_move_the_beam(
    tmp_path: Path, whisper_mock_factory
) -> None:
    # slot 1 is far louder three times, each time for 5 frames (150 ms), with slot 0 alone between
    burst, gap = [5, 1000, 3000], [2, 1000, 0]
    marks, rows = _run_dominant(
        tmp_path, whisper_mock_factory, [QUIET, [10, 1000, 0], burst, gap, burst, gap, burst, gap, TAIL]
    )
    voiced = [m for m in marks if m]
    assert voiced == [SLOT0_MARK] * (10 + 3 * (5 + 2)), marks
    assert len(rows) == 1 and rows[0]["az"] == 0          # slot 0's direction throughout


def test_dominant_mode_slot_that_is_not_clearly_louder_does_not_take_over(
    tmp_path: Path, whisper_mock_factory
) -> None:
    # slot 1 leads for 20 frames but by less than a factor of two
    marks, rows = _run_dominant(tmp_path, whisper_mock_factory, [QUIET, [10, 1000, 0], [20, 1000, 1800], TAIL])
    voiced = [m for m in marks if m]
    assert voiced == [SLOT0_MARK] * 30, marks
    assert len(rows) == 1 and rows[0]["az"] == 0


# --- direction while speaking ---


def _events_while_feeding(stt, directions: list) -> list[dict]:
    """Feed one loud 30 ms frame per entry of `directions` (the tracker's answer at that frame)."""
    import queue

    q: queue.Queue = queue.Queue()
    stt.SUBSCRIBERS.append(q)
    now = {"m": None}
    seg = stt.Segmenter(meta=lambda: now["m"], name="t")
    try:
        for d in directions:
            now["m"] = d
            seg.feed(_pcm(0.03, amp=6000))
    finally:
        stt.SUBSCRIBERS.remove(q)
    out = []
    while not q.empty():
        out.append(q.get())
    return out


def test_direction_found_after_speech_started_is_published_while_still_speaking(rsp_stt_loader) -> None:
    """ODAS tracks a talker a moment after the level rises, so speech_start often has no direction."""
    stt = rsp_stt_loader()
    there = {"src": 2, "az": 120.0, "el": 30.0}
    events = _events_while_feeding(stt, [None] * 20 + [there] * 40)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "speech_start" and "az" not in events[0]
    dirs = [e for e in events if e["event"] == "speech_dir"]
    assert len(dirs) == 1, "one event per direction, not one per frame: %s" % kinds
    assert dirs[0]["az"] == 120 and dirs[0]["src"] == 2


def test_direction_is_published_again_only_when_the_talker_has_moved(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    a = {"src": 2, "az": 350.0, "el": 30.0}
    wobble = {"src": 2, "az": 4.0, "el": 30.0}            # 14 degrees on, across north
    b = {"src": 2, "az": 60.0, "el": 30.0}
    events = _events_while_feeding(stt, [a] * 30 + [wobble] * 30 + [b] * 30)
    assert events[0]["event"] == "speech_start" and events[0]["az"] == 350.0
    assert [e["az"] for e in events if e["event"] == "speech_dir"] == [60]


# --- nothing longer than a couple of seconds disappears without a trace ---


def _feed(seg, seconds: float, amp: int) -> None:
    for _ in range(int(seconds / 0.03)):
        seg.feed(_pcm(0.03, amp=amp))


def _untracked_utterance(stt, seconds: float = 6.0):
    """What the segmenter queues for a sound ODAS never tracked: (frames, meta) or None."""
    seg = stt.Segmenter(meta=lambda: None, name="t")
    _feed(seg, seconds, 6000)
    _feed(seg, 3.0, 0)
    return None if stt.PENDING.empty() else stt.PENDING.get_nowait()


def test_long_sound_the_tracker_never_located_goes_to_the_speech_gate(rsp_stt_loader) -> None:
    """Measured on the board: in a noisy room ODAS loses a talker for seconds at a time. Those
    seconds used to be thrown away as noise; the speech gate is the better judge of that."""
    stt = rsp_stt_loader()
    item = _untracked_utterance(stt)
    assert item is not None, "six seconds of sound were discarded without asking the speech gate"
    frames, meta = item
    assert meta.get("untracked") is True and "az" not in meta


def test_untracked_speech_is_transcribed(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader()
    frames, meta = _untracked_utterance(stt)
    sent: list[int] = []
    monkeypatch.setattr(stt, "speech_gate_on", lambda: True)
    monkeypatch.setattr(stt, "has_speech", lambda pcm: True)
    monkeypatch.setattr(stt, "transcribe", lambda pcm: sent.append(len(pcm)) or "some words")
    stt.handle(frames, meta)
    assert len(sent) == 1


def test_untracked_noise_is_dropped_by_the_gate_and_leaves_a_trace(rsp_stt_loader, monkeypatch, capsys) -> None:
    stt = rsp_stt_loader()
    frames, meta = _untracked_utterance(stt)
    monkeypatch.setattr(stt, "speech_gate_on", lambda: True)
    monkeypatch.setattr(stt, "has_speech", lambda pcm: False)
    monkeypatch.setattr(stt, "transcribe", lambda pcm: pytest.fail("noise was sent to Whisper"))
    stt.handle(frames, meta)
    assert re.search(r"drop: no speech in 6\.\ds utterance", capsys.readouterr().out)


def test_without_a_speech_gate_untracked_sounds_are_never_sent(rsp_stt_loader, monkeypatch, capsys) -> None:
    """Whisper writes sentences for noise; with nothing to check for speech, keep the old rule."""
    stt = rsp_stt_loader()
    frames, meta = _untracked_utterance(stt)
    monkeypatch.setattr(stt, "speech_gate_on", lambda: False)
    monkeypatch.setattr(stt, "transcribe", lambda pcm: pytest.fail("unchecked noise was sent to Whisper"))
    stt.handle(frames, meta)
    assert re.search(r"drop: .*6\.\ds.*untracked", capsys.readouterr().out)


def test_untracked_sounds_can_be_dropped_outright(rsp_stt_loader, capsys) -> None:
    stt = rsp_stt_loader(env={"STT_UNTRACKED_MIN_MS": "0"})
    assert _untracked_utterance(stt) is None
    assert re.search(r"drop: .*6\.\ds.*untracked", capsys.readouterr().out)


def test_short_untracked_sounds_stay_out_of_the_log(rsp_stt_loader, capsys) -> None:
    stt = rsp_stt_loader()
    seg = stt.Segmenter(meta=lambda: None, name="t")
    _feed(seg, 0.6, 6000)
    _feed(seg, 3.0, 0)
    assert "drop" not in capsys.readouterr().out


def test_long_utterance_that_whisper_returns_empty_is_logged(rsp_stt_loader, monkeypatch, capsys) -> None:
    stt = rsp_stt_loader()
    _script_transcripts(monkeypatch, stt, [""])
    stt.handle([_pcm(5.0)], {"t": 9.0, "az": 90})
    assert re.search(r"drop: .*5\.0s.*empty transcript", capsys.readouterr().out)


def test_odas_runs_sequentially_unless_told_otherwise(rsp_stt_loader) -> None:
    """Measured on the board: odaslive's own threads cost a third of its CPU time in wake-ups.
    With -s it gave the same transcripts on a ten-minute take for 42% less CPU."""
    stt = rsp_stt_loader()
    cmd = stt.odas_command("/run/x.cfg")
    assert cmd[0] == stt.ODAS_BIN and cmd[-2:] == ["-c", "/run/x.cfg"]
    assert "-s" in cmd
    threaded = rsp_stt_loader(env={"STT_ODAS_ARGS": ""})
    assert threaded.odas_command("/run/x.cfg") == [threaded.ODAS_BIN, "-c", "/run/x.cfg"]


def test_odas_level_threshold_follows_what_was_measured(rsp_stt_loader) -> None:
    """ODAS's noise-masked stream is quiet. At the plain-microphone threshold (150) speech in a
    humming room lost about one word in five; at 40 one in nine, with no false transcripts on
    six minutes of fan noise and none of the board's own voice let through."""
    stt = rsp_stt_loader()
    assert stt.ODAS_MIN_RMS == 40
    assert stt.MIN_RMS == 150, "the plain-microphone mode keeps its own threshold"
    assert rsp_stt_loader(env={"STT_ODAS_MIN_RMS": "90"}).ODAS_MIN_RMS == 90


# --- one-microphone mode (STT_ODAS_MODE=mic) ---

MIC_MARK = 3000                # amplitude of the talker in microphone 0 of the recording

# This fake odaslive hears nothing: it reads its input to the end, as ODAS does, and sends
# silence on both streams for as long as the recording lasts. FAKE_ODAS_TRACK=1 makes it
# report one tracked source, straight ahead.
DEAF_ODASLIVE = r"""
import json, os, re, socket, sys, threading, time
import numpy as np
cfg = open(sys.argv[sys.argv.index('-c') + 1]).read()
tp, sp, ap = [int(p) for p in re.findall(r'port = (\d+);', cfg)][:3]
path = re.search(r'type = "file"; path = "([^"]+)"', cfg).group(1)
st = socket.create_connection(('127.0.0.1', tp), timeout=5)
ss = socket.create_connection(('127.0.0.1', sp), timeout=5)
sa = socket.create_connection(('127.0.0.1', ap), timeout=5)
if os.environ.get('FAKE_ODAS_TRACK') == '1':
    st.sendall((json.dumps({'src': [{'id': 1, 'x': 1.0, 'y': 0.0, 'z': 0.0, 'activity': 0.9}]}) + '\n').encode())
    time.sleep(0.3)
silent = np.zeros((480, 4), dtype='<i2').tobytes()
step = 480 * 8 * 4
with open(path, 'rb') as f:
    while len(f.read(step)) == step:
        ss.sendall(silent); sa.sendall(silent)
sa.close(); ss.close(); st.close()
"""

# stand-in canceller: passes everything through unchanged
PASS_AEC = "#!/bin/sh\nexec cat\n"


def _mic_recording(frames_quiet: int, frames_talk: int, frames_tail: int, play: tuple[int, int] | None = None) -> bytes:
    """8 channels S32. Microphone 0 carries the talker; microphone 1 something else, louder.
    `play` = (first frame, frames): the board's speaker plays then (loopback channels 6, 7)."""
    n = (frames_quiet + frames_talk + frames_tail) * 480
    a = np.zeros((n, 8), dtype="<i4")
    talk = slice(frames_quiet * 480, (frames_quiet + frames_talk) * 480)
    square = np.where(np.arange(frames_talk * 480) % 2 == 0, 1, -1)
    a[talk, 0] = square * (MIC_MARK << 16)
    a[talk, 1] = square * (9000 << 16)
    if play:
        # noise for both, as unlike each other as a voice and the board's speech are: steady
        # tones would all look alike to the check for the board's own sound
        rng = np.random.default_rng(7)
        a[talk, 0] = rng.integers(MIC_MARK // 2, MIC_MARK, frames_talk * 480) * square << 16
        span = slice(play[0] * 480, (play[0] + play[1]) * 480)
        a[span, 6] = a[span, 7] = rng.integers(-(2**27), 2**27, play[1] * 480).astype("<i4")
    return a.tobytes()


def _run_mode(tmp_path: Path, whisper_mock_factory, mode: str, canceller: bool = True, track: bool = False, extra=None,
              recording_bytes: bytes | None = None, aec_script: str | None = None):
    base_url, captured = whisper_mock_factory("someone is talking")
    fake = tmp_path / "deaf_odaslive.py"
    fake.write_text(DEAF_ODASLIVE, encoding="utf-8")
    fake_bin = tmp_path / "deaf-odaslive"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable!r} {str(fake)!r} \"$@\"\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    aec = tmp_path / "rsp-aec"
    if canceller:
        aec.write_text(aec_script or PASS_AEC, encoding="utf-8")
        aec.chmod(0o755)
    recording = tmp_path / "take.raw"
    recording.write_bytes(recording_bytes or _mic_recording(20, 40, 30))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log_file = tmp_path / "transcripts.jsonl"
    track_port, audio_port, sep_port = free_ports(3)
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_ODAS_TRACK": "1" if track else "0",
            "STT_SOURCE": "odas",
            "STT_ODAS_MODE": mode,
            "STT_AEC_BIN": str(aec),
            "STT_ODAS_INPUT": str(recording),
            "STT_ODAS_BIN": str(fake_bin),
            "STT_ODAS_CFG": str(BOARD_DIR / "odas-respeaker-core-v2.cfg"),
            "STT_ODAS_TRACK_PORT": str(track_port),
            "STT_ODAS_AUDIO_PORT": str(audio_port),
            "STT_ODAS_SEP_PORT": str(sep_port),
            "STT_SERVER": f"{base_url}/inference",
            "STT_RUN_DIR": str(run_dir),
            "STT_REC_DIR": str(tmp_path / "rec"),
            "STT_LOG": str(log_file),
            "STT_HTTP": "",
            "STT_SPEECH_MIN_MS": "0",
            "STT_PREROLL_MS": "300",
            "STT_END_SILENCE_MS": "150",
            "STT_MIN_UTTERANCE_MS": "150",
            "STT_MIN_DYN_DB": "0",
            **(extra or {}),
        }
    )
    res = run_bounded([sys.executable, "-u", str(BOARD_DIR / "rsp-stt.py")], env=env, timeout=60)
    assert res.returncode == 0, f"rsp-stt.py failed:\n{res.stdout}\n{res.stderr}"
    marks = []
    for c in captured:
        fields = _multipart_fields(c["content_type"], c["body"])
        with wave.open(io.BytesIO(fields["file"]), "rb") as w:
            samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        marks.append([int(np.abs(f).max()) for f in samples[: len(samples) // 480 * 480].reshape(-1, 480)])
    return marks, _logged(log_file), res.stdout + res.stderr


def test_mic_mode_sends_the_microphone_when_odas_hears_and_tracks_nothing(tmp_path: Path, whisper_mock_factory) -> None:
    marks, rows, _ = _run_mode(tmp_path, whisper_mock_factory, "mic")
    assert len(marks) == 1, "the talker in the microphone must become one utterance"
    voiced = [m for m in marks[0] if m]
    assert set(voiced) == {MIC_MARK}, "what is sent is microphone 0, nothing else"
    assert len(voiced) == 40, "all 40 frames of the talker, from the first one"
    assert len(rows) == 1 and rows[0]["text"] == "someone is talking" and "az" not in rows[0]


def test_dominant_mode_loses_the_same_talker(tmp_path: Path, whisper_mock_factory) -> None:
    """The reason for mic mode: with ODAS silent, the dominant mode has nothing to go on."""
    marks, rows, _ = _run_mode(tmp_path, whisper_mock_factory, "dominant")
    assert marks == [] and rows == []


def test_mic_mode_reports_the_direction_when_odas_has_one(tmp_path: Path, whisper_mock_factory) -> None:
    marks, rows, _ = _run_mode(tmp_path, whisper_mock_factory, "mic", track=True)
    assert len(rows) == 1 and rows[0]["az"] == 0 and rows[0]["src"] == 1


def test_mic_mode_can_use_another_microphone(tmp_path: Path, whisper_mock_factory) -> None:
    marks, _, _ = _run_mode(tmp_path, whisper_mock_factory, "mic", extra={"STT_MIC_CHANNEL": "1"})
    assert len(marks) == 1 and set(m for m in marks[0] if m) == {9000}


def test_mic_mode_without_the_native_canceller_says_so_and_uses_dominant_mode(tmp_path: Path, whisper_mock_factory) -> None:
    marks, rows, log = _run_mode(tmp_path, whisper_mock_factory, "mic", canceller=False)
    assert "mic mode needs the native canceller" in log
    assert marks == [] and rows == []


def test_a_talker_still_speaking_when_a_replay_ends_is_not_lost(tmp_path: Path, whisper_mock_factory) -> None:
    """A recording can stop in the middle of a sentence; the take must still be scored on it."""
    marks, rows, _ = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=_mic_recording(20, 40, 0))
    assert len(marks) == 1 and len([m for m in marks[0] if m]) == 40
    assert len(rows) == 1


def test_mic_mode_cuts_an_utterance_where_the_boards_speaker_starts(tmp_path: Path, whisper_mock_factory) -> None:
    """One stream carries the person and the board. What was said before the board started
    must not be judged together with what came after."""
    # the talker speaks during frames 20-99; the board plays from frame 60 to the end
    take = _mic_recording(20, 80, 30, play=(60, 70))
    marks, rows, log = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=take)
    voiced = [len([m for m in u if m]) for u in marks]
    assert len(voiced) == 2, log
    assert sum(voiced) == 80, "no frame of the talker is lost at the cut"
    assert abs(voiced[0] - 40) <= 1, f"the first piece ends where the speaker starts: {voiced}"


def test_mic_mode_without_playback_keeps_one_utterance(tmp_path: Path, whisper_mock_factory) -> None:
    marks, _, _ = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=_mic_recording(20, 80, 30))
    assert len(marks) == 1 and len([m for m in marks[0] if m]) == 80


# stand-in canceller that cancels: the echo in microphone 0 is four times loopback channel 7,
# and it takes exactly that away. --gain DB is applied afterwards, as rsp-aec does.
CANCELLING_AEC = f"""#!{sys.executable}
import sys
import numpy as np
gain = 10 ** (float(sys.argv[sys.argv.index('--gain') + 1]) / 20) if '--gain' in sys.argv else 1.0
step = 480 * 8 * 4
while True:
    raw = sys.stdin.buffer.read(step)
    if len(raw) < step:
        break
    a = np.frombuffer(raw, dtype='<i4').reshape(-1, 8).astype(np.int64)
    a[:, 0] = np.clip((a[:, 0] - 4 * a[:, 6]) * gain, -2**31, 2**31 - 1)
    sys.stdout.buffer.write(a.astype('<i4').tobytes())
    sys.stdout.buffer.flush()
"""


def _board_recording(frames: int, play: tuple[int, int], talk: tuple[int, int] | None = None) -> bytes:
    """8 channels S32. The board's speaker plays during `play` = (first frame, frames) and is
    heard in microphone 0, with a distorted part (8% of it) that no canceller can take away
    and that resembles nothing that was played. `talk`: a person, in microphone 0 as well."""
    rng = np.random.default_rng(11)
    a = np.zeros((frames * 480, 8), dtype=np.int64)
    span = slice(play[0] * 480, (play[0] + play[1]) * 480)
    played = rng.integers(-(2**27), 2**27, play[1] * 480)
    a[span, 6] = a[span, 7] = played
    a[span, 0] = 4 * played + rng.integers(-(2**29), 2**29, play[1] * 480) * 8 // 100
    if talk:
        said = slice(talk[0] * 480, (talk[0] + talk[1]) * 480)
        a[said, 0] += rng.integers(-6000, 6000, talk[1] * 480) << 16
    return a.astype("<i4").tobytes()


def test_mic_mode_does_not_send_what_the_canceller_leaves_of_the_boards_voice(tmp_path: Path, whisper_mock_factory) -> None:
    """The leftover is loud enough to be taken for speech and matches nothing that was played.
    It is still only a small part of what the microphone heard, and that gives it away."""
    take = _board_recording(160, play=(20, 100))
    marks, rows, log = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=take, aec_script=CANCELLING_AEC)
    assert marks == [] and rows == [], log


def test_mic_mode_knows_the_leftover_when_the_canceller_adds_gain(tmp_path: Path, whisper_mock_factory) -> None:
    take = _board_recording(160, play=(20, 100))
    marks, rows, log = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=take, aec_script=CANCELLING_AEC,
                                 extra={"STT_AEC_ARGS": "--gain 10"})
    assert marks == [] and rows == [], log


def test_mic_mode_sends_a_person_talking_over_the_board(tmp_path: Path, whisper_mock_factory) -> None:
    take = _board_recording(200, play=(20, 150), talk=(60, 60))
    marks, rows, log = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=take, aec_script=CANCELLING_AEC,
                                 extra={"STT_PLAY_END_SILENCE_MS": "150"})
    assert len(marks) == 1 and len(rows) == 1, log
    # the person's 60 frames, the 300 ms before them and the silence that ended the utterance
    assert 60 <= len(marks[0]) <= 60 + 10 + 6, len(marks[0])


def test_mic_mode_hears_a_short_word_said_over_the_board(tmp_path: Path, whisper_mock_factory) -> None:
    take = _board_recording(200, play=(20, 150), talk=(80, 12))
    marks, rows, log = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=take, aec_script=CANCELLING_AEC,
                                 extra={"STT_PLAY_END_SILENCE_MS": "150"})
    assert len(marks) == 1 and len(rows) == 1, log
    assert 12 <= len(marks[0]) <= 12 + 10 + 6, len(marks[0])


def test_mic_mode_waits_longer_for_a_person_to_go_on_while_the_board_speaks(tmp_path: Path, whisper_mock_factory) -> None:
    """Over the board's voice a person is heard in pieces: where the board is louder they
    seem to have stopped. Two pieces half a second apart are one utterance."""
    rng = np.random.default_rng(5)
    a = np.frombuffer(_board_recording(220, play=(20, 180), talk=(60, 30)), dtype="<i4").reshape(-1, 8).copy()
    again = slice(107 * 480, 137 * 480)
    a[again, 0] += (rng.integers(-6000, 6000, 30 * 480) << 16).astype("<i4")
    marks, rows, log = _run_mode(tmp_path, whisper_mock_factory, "mic", recording_bytes=a.tobytes(), aec_script=CANCELLING_AEC)
    assert len(marks) == 1 and len(rows) == 1, log
