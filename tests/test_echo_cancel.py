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


def test_the_idle_noise_of_the_loopback_channels_does_not_start_the_canceller(rsp_stt_loader) -> None:
    """Measured on the board: with the speaker idle the loopback is not zero but a few steps of
    noise (up to 28672 of 2**31 per two channels). Treating that as playback kept the canceller,
    and most of a CPU core, busy all the time."""
    stt = rsp_stt_loader()
    talk = _speechlike(3, seed=2, level=0.05)
    raw = bytearray(_raw([talk * (1 + 0.1 * c) for c in range(6)], np.zeros(len(talk))))
    a = np.frombuffer(raw, dtype="<i4").reshape(-1, CH)
    a[:, 6:] = np.random.default_rng(7).integers(-3, 4, (len(a), 2)) * 4096
    raw = bytes(raw)
    c = stt.EchoCanceller()
    step = BLOCK * CH * 4
    for i in range(0, len(raw) - step + 1, step):
        assert c.process(raw[i : i + step]) == raw[i : i + step]
    assert not c.active


def test_quiet_playback_still_starts_the_canceller(rsp_stt_loader) -> None:
    """The loopback follows the hardware volume; 40 dB below a normal level must still count."""
    stt = rsp_stt_loader()
    ref = _speechlike(2, seed=8, level=0.0008)
    c = stt.EchoCanceller()
    _run(c, _raw([_room(ref, 12, 6.0)] * 6, ref))
    assert c.active


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
    said: list[dict] = []
    monkeypatch.setattr(stt, "transcribe", lambda pcm: calls.append(len(pcm)) or "words number %d" % len(calls))
    monkeypatch.setattr(stt, "emit", lambda text, dur, took, meta: said.append(meta))
    stt.handle(_pcm16(audio), {"t": end_s, "az": az})
    return bool(said)


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


def test_replaying_a_recording_knows_what_the_speaker_played_in_it(rsp_stt_loader, monkeypatch, tmp_path) -> None:
    """rsp-replay must judge a take as the live service would: the recording's loopback channels
    are the played signal, so the board's own voice in it is recognised offline too."""
    stt = rsp_stt_loader()
    ref, heard = _scene()
    quiet = np.zeros(2 * RATE)
    raw = _raw([np.concatenate([quiet, heard])] * 6, np.concatenate([quiet, ref]))
    take = tmp_path / "take.raw"
    take.write_bytes(raw)
    stt.preload_played(str(take))
    assert stt.PLAYED.n == len(raw) // (BLOCK * CH * 4)
    assert stt.PLAYED.blocks[10] is None and stt.PLAYED.blocks[166] is not None   # 0.3 s idle, 5 s playing
    assert _handle(stt, monkeypatch, heard[RATE : 7 * RATE], end_s=9.0) == [], "the board's own voice was sent to Whisper"


# --- talking over the board from across the room: the leftover is a large part of the utterance ---


def _over_the_speaker(heard: np.ndarray, seed: int, share: float) -> np.ndarray:
    """A talker with the board's leftover under them; the leftover is `share` of the energy."""
    seg = heard[RATE : 7 * RATE]
    other = _speechlike(6, seed=seed, level=0.3)
    return seg * (np.sqrt(share / (1 - share)) * other.std() / seg.std()) + other


def test_a_talker_elsewhere_is_kept_with_a_lot_of_the_speaker_under_them(rsp_stt_loader, monkeypatch) -> None:
    """Measured on the board: a person across the room while it spoke 25 dB louder gave 35-45%."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    assert _hand(stt, monkeypatch, _over_the_speaker(heard, 60, 0.4), 7.0, az=65)


def test_mostly_speaker_is_dropped_wherever_it_seems_to_come_from(rsp_stt_loader, monkeypatch) -> None:
    """The board alone was sometimes tracked at a reflection, with 69-76% of it played sound."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    assert not _hand(stt, monkeypatch, _over_the_speaker(heard, 61, 0.75), 7.0, az=65)


def test_the_same_mix_without_a_direction_is_dropped(rsp_stt_loader, monkeypatch) -> None:
    """Untracked sound has no direction to speak for it: the strict limit applies."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    calls: list[int] = []
    monkeypatch.setattr(stt, "transcribe", lambda pcm: calls.append(len(pcm)) or "some words")
    monkeypatch.setattr(stt, "has_speech", lambda pcm: True)
    monkeypatch.setattr(stt, "speech_gate_on", lambda: True)
    stt.handle(_pcm16(_over_the_speaker(heard, 60, 0.4)), {"t": 7.0, "untracked": True})
    assert calls == []


def test_the_same_mix_from_the_speakers_direction_is_dropped(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    assert not _hand(stt, monkeypatch, _over_the_speaker(heard, 60, 0.4), 7.0, az=178)


def test_a_configured_speaker_direction_needs_no_learning(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    assert not _hand(stt, monkeypatch, _faint_leftover(heard, seed=50), 7.0, az=190)


def test_a_reflection_does_not_move_the_configured_speaker_direction(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    assert not _hand(stt, monkeypatch, heard[RATE : 7 * RATE], 7.0, az=70)       # clear echo, heard off a wall
    assert _hand(stt, monkeypatch, _over_the_speaker(heard, 62, 0.4), 7.0, az=65), "the talker near that wall is kept"


# --- which beam to follow while the board speaks ---


def test_while_the_board_speaks_a_person_elsewhere_is_followed_not_the_speaker(rsp_stt_loader) -> None:
    """Live, the speaker's beam was the loudest for the whole of a 70 s reading and the person
    talking over it from 74 degrees never got a transcript."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    tracks = [{"az": 170.0}, {"az": 74.0}, None, None]
    assert stt.without_speaker([900.0, 200.0, 5.0, 5.0], tracks, True) == [0.0, 200.0, 5.0, 5.0]


def test_the_loudest_beam_is_followed_as_before_when_the_board_is_silent(rsp_stt_loader) -> None:
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    tracks = [{"az": 170.0}, {"az": 74.0}, None, None]
    assert stt.without_speaker([900.0, 200.0, 5.0, 5.0], tracks, False) == [900.0, 200.0, 5.0, 5.0]


def test_the_speakers_beam_is_kept_when_nothing_else_is_tracked(rsp_stt_loader) -> None:
    """Otherwise its leftover would be followed in an untracked slot and never be recognised by direction."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    assert stt.without_speaker([900.0, 20.0], [{"az": 170.0}, None], True) == [900.0, 20.0]
    assert stt.without_speaker([900.0, 20.0], [{"az": 60.0}, {"az": 80.0}], True) == [900.0, 20.0]


def test_playing_is_known_per_moment_of_the_stream(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    _play(stt, np.concatenate([np.zeros(RATE), _speechlike(2, seed=3, level=0.3), np.zeros(3 * RATE)]))
    stt.PLAYED.blocks = type(stt.PLAYED.blocks)([None if not b.any() else b for b in stt.PLAYED.blocks], maxlen=4000)
    assert not stt.PLAYED.playing(20)            # 0.6 s in: nothing yet
    assert stt.PLAYED.playing(70)                # 2.1 s in: playing
    assert not stt.PLAYED.playing(180)           # 5.4 s in: long over


# --- a person over the board: what the board said is taken out of their transcript ---


def test_the_boards_words_are_taken_out_of_a_transcript(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    left, gone = stt.without_played(
        "Every market town keeps one stall that sells nothing extra.\n When we were kids, there was a strange house.",
        "Every market town keeps one stall that sells nothing but string. This one belongs to a woman named Odile.",
    )
    assert left == "extra.\n When we were kids, there was a strange house."
    assert gone == 9


def test_single_common_words_are_not_taken_for_the_boards(rsp_stt_loader) -> None:
    stt = rsp_stt_loader()
    assert stt.without_played("I put the kettle on.", "The night train leaves on time.") == ("I put the kettle on.", 0)


def _hand_text(stt, monkeypatch, audio, az, texts: list[str]) -> list[dict]:
    """One utterance; Whisper answers with `texts` in order. Returns what was published."""
    answers, out = list(texts), []
    monkeypatch.setattr(stt, "transcribe", lambda pcm: answers.pop(0))
    monkeypatch.setattr(stt, "has_speech", lambda pcm: True)
    monkeypatch.setattr(stt, "emit", lambda text, dur, took, meta: out.append(dict(meta, text=text)))
    stt.handle(_pcm16(audio), {"t": 7.0, "az": az})
    return out


BOARD = "The night train to the mountains leaves from platform nine and it is never quite on time."
PERSON = "When we were kids there was a strange house and all the shades were always drawn."


def test_a_person_over_the_board_is_kept_even_when_most_of_the_sound_is_the_board(rsp_stt_loader, monkeypatch) -> None:
    """Live, a clearly audible reading scored 55-67% played sound: no limit on the sound separates the two."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    out = _hand_text(stt, monkeypatch, _over_the_speaker(heard, 60, 0.62), 74, [PERSON, BOARD])
    assert [o["text"] for o in out] == [PERSON]
    assert out[0]["over_speaker"] is True


def test_the_boards_words_do_not_reach_the_persons_transcript(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    mixed = "The night train to the mountains leaves from hot brown wine. " + PERSON
    out = _hand_text(stt, monkeypatch, _over_the_speaker(heard, 60, 0.4), 74, [mixed, BOARD])
    assert [o["text"] for o in out] == ["hot brown wine. " + PERSON]


def test_the_board_alone_heard_off_a_wall_is_dropped_by_its_words(rsp_stt_loader, monkeypatch, capsys) -> None:
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    assert _hand_text(stt, monkeypatch, _over_the_speaker(heard, 60, 0.4), 74, [BOARD, BOARD]) == []
    assert "drop: own speaker" in capsys.readouterr().out


def test_a_few_garbled_words_of_mostly_played_sound_are_dropped(rsp_stt_loader, monkeypatch) -> None:
    """The board alone, tracked at a reflection, gave 'All by our heart.' at 69% played sound."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    assert _hand_text(stt, monkeypatch, _over_the_speaker(heard, 61, 0.75), 74, ["All by our heart.", BOARD]) == []


def test_whisper_is_not_asked_twice_when_the_speaker_is_silent(rsp_stt_loader, monkeypatch) -> None:
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    _play(stt, np.zeros(10 * RATE))
    stt.PLAYED.blocks = type(stt.PLAYED.blocks)([None] * len(stt.PLAYED.blocks), maxlen=4000)
    out = _hand_text(stt, monkeypatch, _speechlike(6, seed=10, level=0.3), 74, [PERSON])
    assert [o["text"] for o in out] == [PERSON] and "over_speaker" not in out[0]


# --- mic mode: a person ODAS has no track for ---


def _hand_untracked(stt, monkeypatch, audio, texts: list[str]) -> list[dict]:
    answers, out = list(texts), []
    monkeypatch.setattr(stt, "transcribe", lambda pcm: answers.pop(0))
    monkeypatch.setattr(stt, "has_speech", lambda pcm: True)
    monkeypatch.setattr(stt, "emit", lambda text, dur, took, meta: out.append(dict(meta, text=text)))
    stt.handle(_pcm16(audio), {"t": 7.0})
    return out


def _mic_mode_env(tmp_path) -> dict[str, str]:
    exe = tmp_path / "rsp-aec"
    exe.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    exe.chmod(0o755)
    return {"STT_SPEAKER_AZ": "185", "STT_SOURCE": "odas", "STT_ODAS_MODE": "mic", "STT_AEC_BIN": str(exe)}


def test_in_mic_mode_an_untracked_person_over_the_board_is_judged_by_their_words(
    rsp_stt_loader, monkeypatch, tmp_path
) -> None:
    """ODAS loses a talker for seconds at a time. In mic mode their sound still arrives, and
    while the board plays it must not be thrown away for having no direction."""
    stt = rsp_stt_loader(env=_mic_mode_env(tmp_path))
    ref, heard = _scene()
    _play(stt, ref)
    mixed = "The night train to the mountains leaves from hot brown wine. " + PERSON
    out = _hand_untracked(stt, monkeypatch, _over_the_speaker(heard, 60, 0.4), [mixed, BOARD])
    assert [o["text"] for o in out] == ["hot brown wine. " + PERSON]
    assert out[0]["over_speaker"] is True and "az" not in out[0]


def test_in_mic_mode_the_board_alone_without_a_track_is_still_dropped(rsp_stt_loader, monkeypatch, tmp_path, capsys) -> None:
    stt = rsp_stt_loader(env=_mic_mode_env(tmp_path))
    ref, heard = _scene()
    _play(stt, ref)
    assert _hand_untracked(stt, monkeypatch, _over_the_speaker(heard, 60, 0.4), [BOARD, BOARD]) == []
    assert "drop: own speaker" in capsys.readouterr().out


def test_in_dominant_mode_an_untracked_sound_full_of_played_sound_is_dropped_unheard(
    rsp_stt_loader, monkeypatch
) -> None:
    """The rule mic mode changes: without it, no direction means no second look."""
    stt = rsp_stt_loader(env={"STT_SPEAKER_AZ": "185"})
    ref, heard = _scene()
    _play(stt, ref)
    asked: list[int] = []
    monkeypatch.setattr(stt, "transcribe", lambda pcm: asked.append(1) or PERSON)
    monkeypatch.setattr(stt, "has_speech", lambda pcm: True)
    out: list[dict] = []
    monkeypatch.setattr(stt, "emit", lambda text, dur, took, meta: out.append(meta))
    stt.handle(_pcm16(_over_the_speaker(heard, 60, 0.4)), {"t": 7.0})
    assert out == [] and asked == []
