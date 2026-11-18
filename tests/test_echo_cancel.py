"""Echo cancellation in board/rsp-stt.py: the board must not hear its own speaker.

The sound card's last two channels carry what the speaker is playing. The canceller takes the
raw 8-channel blocks on their way to ODAS and removes that from the six microphone channels.
Signals here are synthetic: a known "speaker" signal, a known room echo of it in the mics,
and a known talker, so the expected outcome does not come from the code under test.
"""
from __future__ import annotations

import ctypes.util
import re

import numpy as np
import pytest

RATE, CH, BLOCK = 16000, 8, 480          # the service hands over 30 ms blocks

pytestmark = pytest.mark.skipif(
    ctypes.util.find_library("speexdsp") is None,
    reason="libspeexdsp not installed: echo cancellation cannot run on this host",
)


def _speechlike(seconds: float, seed: int, level: float) -> np.ndarray:
    """Noise in the speech band with syllable-rate loudness changes, peak about `level` of full scale."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    x = rng.standard_normal(n)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / RATE)
    spec[(f < 200) | (f > 3500)] = 0
    x = np.fft.irfft(spec, n)
    # irregular syllables: a random loudness curve, different for every seed
    steps = rng.uniform(0.05, 1.0, n // 2400 + 2)
    x *= np.interp(np.arange(n), np.arange(len(steps)) * 2400, steps)
    return x / np.abs(x).max() * level


def _room(ref: np.ndarray, delay: int, gain: float) -> np.ndarray:
    """What a microphone picks up of the speaker: delayed, louder, with a short decaying tail."""
    h = np.zeros(400)
    h[delay] = 1.0
    h[delay + 7], h[delay + 31], h[delay + 120], h[delay + 260] = 0.5, -0.3, 0.15, -0.08
    return np.convolve(ref, h)[: len(ref)] * gain


def _raw(mics: list[np.ndarray], ref: np.ndarray) -> bytes:
    a = np.zeros((len(ref), CH), dtype=np.int64)
    for c, m in enumerate(mics):
        a[:, c] = np.round(m * 2**31)
    a[:, 6] = a[:, 7] = np.round(ref * 2**31)
    return np.clip(a, -2**31, 2**31 - 1).astype("<i4").tobytes()


def _run(canceller, raw: bytes) -> np.ndarray:
    step = BLOCK * CH * 4
    out = b"".join(canceller.process(raw[i : i + step]) for i in range(0, len(raw) - step + 1, step))
    return np.frombuffer(out, dtype="<i4").reshape(-1, CH).astype(np.float64) / 2**31


def _db(x: np.ndarray) -> float:
    return 20 * np.log10(x.std() + 1e-12)


def test_speaker_sound_is_removed_from_every_microphone(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    ref = _speechlike(12, seed=1, level=0.08)
    mics = [_room(ref, delay=10 + 2 * c, gain=6.0 + c) for c in range(6)]
    out = _run(stt.EchoCanceller(), _raw(mics, ref))
    late = slice(8 * RATE, 11 * RATE)                       # after it has had time to adapt
    for c in range(6):
        removed = _db(mics[c][late]) - _db(out[late, c])
        assert removed > 12, f"mic {c}: only {removed:.1f} dB of the echo removed"


def test_audio_passes_untouched_bit_for_bit_while_the_speaker_is_silent(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    talk = _speechlike(3, seed=2, level=0.05)
    raw = _raw([talk * (1 + 0.1 * c) for c in range(6)], np.zeros(len(talk)))
    c = stt.EchoCanceller()
    step = BLOCK * CH * 4
    for i in range(0, len(raw) - step + 1, step):
        assert c.process(raw[i : i + step]) == raw[i : i + step]
    assert not c.active


def test_a_talker_is_kept_while_the_speaker_plays(rsp_stt_loader) -> None:
    """Barge-in: someone speaking over the board's own voice must come through."""
    stt = rsp_stt_loader()
    ref = _speechlike(12, seed=3, level=0.08)
    talk = np.zeros(len(ref))
    talk[8 * RATE : 11 * RATE] = _speechlike(3, seed=4, level=0.2)
    mics = [_room(ref, delay=12, gain=6.0) + talk for _ in range(6)]
    out = _run(stt.EchoCanceller(), _raw(mics, ref))
    late = slice(8 * RATE + 4000, 11 * RATE - 4000)
    kept = _db(out[late, 0]) - _db(talk[late])
    assert -3 < kept < 3, f"talker level changed by {kept:.1f} dB"
    corr = np.corrcoef(out[late, 0], talk[late])[0, 1]
    assert corr > 0.9, f"output is no longer mostly the talker (correlation {corr:.2f})"


def test_reports_that_the_speaker_is_active_and_for_a_moment_after(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    ref = np.concatenate([_speechlike(1, seed=5, level=0.08), np.zeros(RATE)])
    c = stt.EchoCanceller()
    raw = _raw([np.zeros(len(ref))] * 6, ref)
    step = BLOCK * CH * 4
    seen = []
    for i in range(0, len(raw) - step + 1, step):
        c.process(raw[i : i + step])
        seen.append(c.active)
    assert seen[10] and seen[32], "active while playing"
    assert seen[36], "still active just after, while the room rings on"
    assert not seen[-1], "idle again a second later"


def test_the_reference_channels_are_left_as_they_were(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    ref = _speechlike(2, seed=6, level=0.08)
    raw = _raw([_room(ref, 12, 6.0)] * 6, ref)
    out = _run(stt.EchoCanceller(), raw)
    want = np.frombuffer(raw, dtype="<i4").reshape(-1, CH)[: len(out), 6:].astype(np.float64) / 2**31
    assert np.array_equal(out[:, 6:], want)


# --- what is left of the board's own voice after cancellation must not become a transcript ---


def _pcm16(x: np.ndarray) -> list[bytes]:
    """30 ms mono 16-bit frames, as the separated stream arrives at the segmenter."""
    s = np.clip(np.round(x * 32768), -32768, 32767).astype("<i2")
    return [s[i : i + BLOCK].tobytes() for i in range(0, len(s) - BLOCK + 1, BLOCK)]


def _play(stt, ref: np.ndarray) -> None:
    """Tell the service what the speaker played, block by block, as the capture thread does."""
    s = np.clip(np.round(ref * 32768), -32768, 32767).astype(np.int16)
    for i in range(0, len(s) - BLOCK + 1, BLOCK):
        stt.PLAYED.add(s[i : i + BLOCK])


def _handle(stt, monkeypatch, audio: np.ndarray, end_s: float) -> list[int]:
    """Give the uploader one finished utterance that ended at stream time end_s; what reaches Whisper."""
    calls: list[int] = []
    monkeypatch.setattr(stt, "transcribe", lambda pcm: calls.append(len(pcm)) or "some words")
    stt.handle(_pcm16(audio), {"t": end_s, "az": 240})
    return calls


def _scene():
    """Five seconds played from t = 1 s; the separated stream carries it 0.4 s later, coloured by the room."""
    ref = np.concatenate([np.zeros(RATE), _speechlike(5, seed=7, level=0.3), np.zeros(4 * RATE)])
    heard = np.concatenate([np.zeros(6400), _room(ref, delay=20, gain=0.2)])[: len(ref)]
    return ref, heard


def test_leftover_of_the_boards_own_voice_is_dropped_and_logged(rsp_stt_loader, monkeypatch, capsys) -> None:
    stt = rsp_stt_loader()
    ref, heard = _scene()
    _play(stt, ref)
    assert _handle(stt, monkeypatch, heard[RATE : 7 * RATE], end_s=7.0) == []
    assert re.search(r"drop: own speaker", capsys.readouterr().out)


def test_someone_else_talking_while_the_speaker_plays_is_kept(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader()
    ref, _ = _scene()
    _play(stt, ref)
    talker = np.concatenate([np.zeros(RATE), _speechlike(5, seed=9, level=0.3), np.zeros(RATE)])
    assert len(_handle(stt, monkeypatch, talker[RATE : 7 * RATE], end_s=7.0)) == 1


def test_someone_talking_over_a_faint_leftover_is_kept(rsp_stt_loader, monkeypatch) -> None:
    """Barge-in: the talker dominates, the cancelled echo is still faintly there."""
    stt = rsp_stt_loader()
    ref, heard = _scene()
    _play(stt, ref)
    mix = heard * 0.25 + np.concatenate([np.zeros(2 * RATE), _speechlike(4, seed=11, level=0.3), np.zeros(4 * RATE)])
    assert len(_handle(stt, monkeypatch, mix[RATE : 7 * RATE], end_s=7.0)) == 1


def test_short_remark_while_the_speaker_plays_is_kept(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader()
    ref, _ = _scene()
    _play(stt, ref)
    for seed in range(20, 40):
        talker = _speechlike(1.2, seed=seed, level=0.3)
        assert len(_handle(stt, monkeypatch, talker, end_s=4.0)) == 1, f"seed {seed}"


def test_speech_with_a_silent_speaker_is_kept(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader()
    _play(stt, np.zeros(10 * RATE))
    assert len(_handle(stt, monkeypatch, _speechlike(4, seed=10, level=0.3), end_s=6.0)) == 1


# --- a distorted leftover is recognised by where it comes from ---


def _faint_leftover(heard: np.ndarray, seed: int) -> np.ndarray:
    """Mostly distortion that no longer matches what was played, with a little of the echo in it."""
    seg = heard[RATE : 7 * RATE]
    other = _speechlike(6, seed=seed, level=0.3)
    return seg * (0.3 * other.std() / seg.std()) + other          # the echo is about 8% of the energy


def _hand(stt, monkeypatch, audio: np.ndarray, end_s: float, az: int) -> bool:
    calls: list[int] = []
    monkeypatch.setattr(stt, "transcribe", lambda pcm: calls.append(len(pcm)) or "some words")
    stt.handle(_pcm16(audio), {"t": end_s, "az": az})
    return bool(calls)


def test_faint_leftover_from_the_speakers_direction_is_dropped_once_that_direction_is_known(
    rsp_stt_loader, monkeypatch
) -> None:
    stt = rsp_stt_loader()
    ref, heard = _scene()
    _play(stt, ref)
    faint = _faint_leftover(heard, seed=50)
    assert _hand(stt, monkeypatch, faint, 7.0, az=238), "nothing is known about the speaker yet: kept"
    assert not _hand(stt, monkeypatch, heard[RATE : 7 * RATE], 7.0, az=240)      # a clear echo teaches the direction
    assert not _hand(stt, monkeypatch, faint, 7.0, az=238), "same direction as the speaker, during playback"


def test_faint_leftover_under_a_talker_elsewhere_in_the_room_is_kept(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader()
    ref, heard = _scene()
    _play(stt, ref)
    assert not _hand(stt, monkeypatch, heard[RATE : 7 * RATE], 7.0, az=240)
    assert _hand(stt, monkeypatch, _faint_leftover(heard, seed=51), 7.0, az=60)


def test_speech_from_the_speakers_direction_is_kept_when_the_speaker_is_silent(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader()
    ref, heard = _scene()
    _play(stt, ref)
    assert not _hand(stt, monkeypatch, heard[RATE : 7 * RATE], 7.0, az=240)
    _play(stt, np.zeros(20 * RATE))                         # twenty seconds of silence follow
    stt.PLAYED.blocks = type(stt.PLAYED.blocks)([None if not b.any() else b for b in stt.PLAYED.blocks], maxlen=4000)
    assert _hand(stt, monkeypatch, _speechlike(4, seed=52, level=0.3), 25.0, az=240)
