"""The native echo canceller (board/rsp-aec.cc) in the capture path of board/rsp-stt.py.

rsp-aec itself is an ARM program built by newos/cross/build-aec.sh; here a stand-in with the
same interface (raw 8-channel capture on stdin, cleaned capture on stdout) checks how the
service uses it: what reaches ODAS, what a recording keeps, and what happens without it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import BOARD_DIR, NEWOS_DIR

CH, BLOCK = 8, 480                      # 30 ms blocks of 8 channels S32
BYTES = BLOCK * CH * 4
N_BLOCKS = 40


def _capture() -> bytes:
    """1.2 s: microphones carry a ramp, the loopback plays during blocks 10-19 only."""
    a = np.zeros((N_BLOCKS * BLOCK, CH), dtype="<i4")
    a[:, :6] = (np.arange(len(a))[:, None] * 1000 + np.arange(6) * 7) % 2**30
    play = slice(10 * BLOCK, 20 * BLOCK)
    a[play, 6] = a[play, 7] = (np.sin(np.arange(10 * BLOCK) / 5.0) * 2**27).astype("<i4")
    return a.tobytes()


# stand-in: microphones negated (so its work is visible), loopback untouched
FAKE_AEC = """#!/usr/bin/env python3
import sys, numpy as np
open(sys.argv[0] + ".args", "w").write(" ".join(sys.argv[1:]))
while True:
    raw = sys.stdin.buffer.read(160 * 8 * 4)
    if len(raw) < 160 * 8 * 4:
        break
    a = np.frombuffer(raw, dtype="<i4").reshape(-1, 8).copy()
    a[:, :6] = -a[:, :6]
    sys.stdout.buffer.write(a.tobytes()); sys.stdout.buffer.flush()
"""


def _run(tmp_path: Path, env: dict[str, str], record: bool = False) -> tuple[bytes, str, Path]:
    """Run capture_to on the scripted capture; returns (what ODAS was fed, the log, rec dir)."""
    run_dir, rec_dir, bin_dir = tmp_path / "run", tmp_path / "rec", tmp_path / "bin"
    for d in (run_dir, rec_dir, bin_dir):
        d.mkdir()
    (tmp_path / "capture.raw").write_bytes(_capture())
    arecord = bin_dir / "arecord"
    arecord.write_text(f"#!/bin/sh\ncat {tmp_path / 'capture.raw'}\n", encoding="utf-8")
    arecord.chmod(0o755)
    fifo, fed = run_dir / "raw.fifo", tmp_path / "fed.raw"
    os.mkfifo(fifo)
    if record:
        (run_dir / "record.req").write_text("0.3 take\n", encoding="utf-8")
    child = (
        "import importlib.util, os, sys, threading\n"
        f"spec = importlib.util.spec_from_file_location('rsp_stt_c', {str(BOARD_DIR / 'rsp-stt.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "def drain():\n"
        f"    with open({str(fifo)!r}, 'rb', buffering=0) as f, open({str(fed)!r}, 'wb') as o:\n"
        "        while True:\n"
        "            b = f.read(65536)\n"
        "            if not b: break\n"
        "            o.write(b); o.flush()\n"
        "t = threading.Thread(target=drain); t.start()\n"
        "real_exit = os._exit\n"
        "def leave(code):\n"
        "    played = sum(1 for b in m.PLAYED.blocks if b is not None)\n"
        "    print('PLAYED_BLOCKS=%d PLAYBACK_SEEN=%d' % (played, m.PLAYBACK[0] > 0), file=sys.stderr, flush=True)\n"
        "    t.join(5)\n"
        "    real_exit(code)\n"
        "m.os._exit = leave\n"
        f"m.capture_to({str(fifo)!r})\n"
    )
    full_env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
                    STT_RUN_DIR=str(run_dir), STT_REC_DIR=str(rec_dir), **env)
    res = subprocess.run([sys.executable, "-B", "-c", child], env=full_env, capture_output=True, text=True,
                         timeout=30, check=False)
    assert res.returncode == 1, res.stderr                 # the scripted capture ended
    return fed.read_bytes(), res.stdout + res.stderr, rec_dir


def _fake(tmp_path: Path) -> Path:
    exe = tmp_path / "rsp-aec"
    exe.write_text(FAKE_AEC, encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_capture_goes_to_odas_through_the_native_canceller(tmp_path: Path) -> None:
    fed, log, _ = _run(tmp_path, {"STT_AEC_BIN": str(_fake(tmp_path))})
    want = np.frombuffer(_capture(), dtype="<i4").reshape(-1, CH).copy()
    want[:, :6] = -want[:, :6]
    got = np.frombuffer(fed, dtype="<i4").reshape(-1, CH)
    assert len(got) == len(want), "every captured block must come out again"
    assert np.array_equal(got, want)
    assert "rsp-aec" in log, "the log must say which canceller runs"


def test_recordings_keep_the_capture_as_the_microphones_heard_it(tmp_path: Path) -> None:
    """A recording is evidence for later analysis: it must not be the cleaned signal."""
    _, _, rec_dir = _run(tmp_path, {"STT_AEC_BIN": str(_fake(tmp_path))}, record=True)
    takes = list(rec_dir.glob("*-take.raw"))
    assert len(takes) == 1
    raw = _capture()
    data = takes[0].read_bytes()
    assert len(data) == 10 * BYTES
    assert data in raw, "the recording is a piece of the untouched capture"


def test_the_service_still_knows_what_the_speaker_played(tmp_path: Path) -> None:
    """Recognising its own voice in an utterance needs the played signal, whoever cancels."""
    _, log, _ = _run(tmp_path, {"STT_AEC_BIN": str(_fake(tmp_path))})
    assert "PLAYBACK_SEEN=1" in log
    played = int(log.split("PLAYED_BLOCKS=")[1].split()[0])
    assert 10 <= played <= 22, f"10 blocks were played (plus the hold after): {played}"


def test_canceller_options_are_passed_on(tmp_path: Path) -> None:
    exe = _fake(tmp_path)
    _run(tmp_path, {"STT_AEC_BIN": str(exe), "STT_AEC_ARGS": "--gentle 4"})
    assert Path(str(exe) + ".args").read_text() == "--gentle 4"


def test_without_the_native_canceller_the_built_in_one_is_used(tmp_path: Path) -> None:
    pytest.importorskip("ctypes.util")
    import ctypes.util
    if ctypes.util.find_library("speexdsp") is None:
        pytest.skip("libspeexdsp not installed: the built-in canceller cannot run on this host")
    fed, log, _ = _run(tmp_path, {"STT_AEC_BIN": str(tmp_path / "missing")})
    got = np.frombuffer(fed, dtype="<i4").reshape(-1, CH)
    raw = np.frombuffer(_capture(), dtype="<i4").reshape(-1, CH)
    assert len(got) == len(raw)
    assert np.array_equal(got[: 9 * BLOCK], raw[: 9 * BLOCK]), "idle capture passes untouched"
    assert "built-in" in log


def test_echo_cancellation_off_feeds_odas_the_capture_unchanged(tmp_path: Path) -> None:
    fed, _, _ = _run(tmp_path, {"STT_AEC_BIN": str(_fake(tmp_path)), "STT_AEC": "0"})
    assert fed == _capture()


def test_a_canceller_that_dies_stops_the_service_instead_of_starving_odas(tmp_path: Path) -> None:
    exe = tmp_path / "rsp-aec"
    exe.write_text("#!/bin/sh\nhead -c 30720 >/dev/null\nexit 3\n", encoding="utf-8")
    exe.chmod(0o755)
    _, log, _ = _run(tmp_path, {"STT_AEC_BIN": str(exe)})        # _run asserts exit code 1
    assert "rsp-aec ended" in log


# --- the build and the image ---


def test_the_image_installs_the_canceller_where_the_service_looks_for_it() -> None:
    script = (NEWOS_DIR / "mk-sd-image.sh").read_text(encoding="utf-8")
    assert "cross/out/rsp-aec" in script and "/opt/rsp-stt/rsp-aec" in script
    stt = (BOARD_DIR / "rsp-stt.py").read_text(encoding="utf-8")
    assert '"/opt/rsp-stt/rsp-aec"' in stt


def test_the_build_pins_the_library_source_and_enables_neon() -> None:
    build = (NEWOS_DIR / "cross" / "build-aec.sh").read_text(encoding="utf-8")
    assert "sha256sum -c" in build, "the downloaded source must be checked"
    assert "-Dneon=yes" in build, "Debian's build of this library has NEON off; ours must not"
    assert "/board/rsp-aec.cc" in build
    docker = (NEWOS_DIR / "cross" / "Dockerfile").read_text(encoding="utf-8")
    for pkg in ("meson", "libspeexdsp-dev:armhf"):
        assert pkg in docker
