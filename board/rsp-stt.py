#!/usr/bin/env python3
"""ReSpeaker Core v2 speech-to-text client.

Captures the mic array, cuts speech into utterances with an adaptive energy VAD, and
sends each utterance to a whisper.cpp server (POST /inference).
Transcripts go to stdout (journald) and to a JSON-lines file.

Two capture sources (STT_SOURCE):
  arecord  the mics averaged into one stream (default)
  odas     ODAS (odaslive) localises and tracks up to four sound sources and beamforms
           each one into its own stream; every stream gets its own VAD, and transcripts
           carry the source direction (azimuth in degrees, array frame).

Standard library only for the plain arecord source (runs on Python 3.5, the vendor Debian 9
image); the ODAS source and Python 3.13+ need numpy. Settings come from the environment;
see rsp-stt.env.
"""
import array, collections, difflib, http.server, io, json, math, os, queue, re, socket
import socketserver, subprocess, sys, threading
import time, uuid, wave
import urllib.request

# audioop left the standard library in Python 3.13 (Debian 13); use numpy there.
try:
    import audioop
    np = None
except ImportError:
    audioop = None
    import numpy as np
if np is None and os.environ.get("STT_SOURCE") == "odas":
    import numpy as np

def env(name, default, cast=str):
    return cast(os.environ.get(name, default))

SERVER    = env("STT_SERVER", "http://127.0.0.1:8910/inference")
DEVICE    = env("STT_DEVICE", "hw:0,0")
CHANNELS  = env("STT_CHANNELS", 8, int)        # card delivers 6 mics + 2 playback-loopback
MIX       = env("STT_CHANNEL", "sum")          # "sum" = average of the mics, or one channel index
MICS      = env("STT_MICS", 6, int)            # channels 0..MICS-1 are microphones
RATE      = 16000
FRAME_MS  = 30
LOG_DROP_MS = 2000     # dropped sounds at least this long are always logged
AEC       = env("STT_AEC", "1") == "1"   # remove the board's own speaker from the microphones
AEC_BIN   = env("STT_AEC_BIN", "/opt/rsp-stt/rsp-aec")   # native canceller (WebRTC AEC3); built-in one without it
AEC_ARGS  = env("STT_AEC_ARGS", "")      # options for it, see rsp-aec.cc
ECHO_SIM  = env("STT_ECHO_SHARE", 0.1, float)   # utterances with this share of played sound in them are the board's own voice
ECHO_CLEAR = 0.2       # share at which an utterance teaches where the speaker is
ECHO_AZ   = env("STT_ECHO_AZ", 25, float)   # degrees round the speaker's direction treated as the speaker while it plays
PLAYBACK  = [0.0]                        # when the speaker was last heard playing (time.time())
DIR_STEP  = 15         # degrees a talker must move before speech_dir is published again
LANGUAGE  = env("STT_LANGUAGE", "en")
START_RATIO = env("STT_START_RATIO", 3.0, float)   # speech = rms > noise floor * ratio
MIN_RMS   = env("STT_MIN_RMS", 150, int)           # ...and above this absolute level
END_SIL_MS = env("STT_END_SILENCE_MS", 800, int)
PRE_MS    = env("STT_PREROLL_MS", 300, int)
MIN_MS    = env("STT_MIN_UTTERANCE_MS", 400, int)
MAX_S     = env("STT_MAX_UTTERANCE_S", 25, float)
# Past this length an utterance ends at the next short pause (between sentences or clauses)
# rather than running into the hard cap mid-word.
SOFT_S    = env("STT_SOFT_UTTERANCE_S", 12, float)
SOFT_SIL_MS = env("STT_SOFT_SILENCE_MS", 350, int)
LOG_FILE  = env("STT_LOG", os.path.expanduser("~/stt/transcripts.jsonl"))
WEBHOOK   = env("STT_WEBHOOK", "")
DEBUG     = env("STT_DEBUG", "0") == "1"
SOURCE    = env("STT_SOURCE", "arecord")
ODAS_BIN  = env("STT_ODAS_BIN", "/opt/odas/bin/odaslive")
ODAS_ARGS = env("STT_ODAS_ARGS", "-s")   # -s: one thread; its own threads cost a third of its CPU in wake-ups
ODAS_CFG  = env("STT_ODAS_CFG", "/opt/rsp-stt/odas-respeaker-core-v2.cfg")
ODAS_AUDIO_PORT = env("STT_ODAS_AUDIO_PORT", 9002, int)   # "postfiltered" sink in the cfg
ODAS_TRACK_PORT = env("STT_ODAS_TRACK_PORT", 9000, int)   # "tracked" sink in the cfg
ODAS_STREAMS = env("STT_ODAS_STREAMS", 4, int)            # tracked-source slots (ssl.nPots)
ODAS_MIN_RMS = env("STT_ODAS_MIN_RMS", MIN_RMS, int)
ODAS_SEP_PORT = env("STT_ODAS_SEP_PORT", 9003, int)       # "separated" sink in the cfg
# Which ODAS stream is sent to the server. The postfiltered stream (noise-masked) always
# drives the VAD; "separated" is the plain beamformer output, which speech recognisers
# usually prefer. "both" transcribes each and logs the postfiltered text as "text_pf".
ODAS_SEND = env("STT_ODAS_SEND", "separated")
# Directions to ignore, "azimuth:halfwidth" in degrees, comma separated (e.g. a PC fan).
ODAS_IGNORE = [tuple(float(v) for v in p.split(":")) for p in
               env("STT_ODAS_IGNORE", "").split(",") if ":" in p]
# Steady noise has a flat level; speech swings by 15 dB or more between syllables and gaps.
# Utterances whose frame-level spread (95th vs 25th percentile, dB) is below this are dropped.
MIN_DYN_DB = env("STT_MIN_DYN_DB", 0, float)
# How the ODAS source streams become utterances:
#   dominant  one output stream that follows whichever tracked source is loudest. One talker
#             moving about (tracks hand over, reflections get tracked) stays one utterance.
#   separate  each tracked source is segmented on its own; simultaneous talkers come out as
#             separate transcripts, but one talker can show up twice or be split.
ODAS_MODE = env("STT_ODAS_MODE", "dominant")
# Where ODAS gets its audio. Empty: this program captures the sound card with arecord and
# feeds ODAS through a FIFO, so it can also save the raw capture on request (rsp-record)
# and a capture overrun does not end the pipeline. A path: replay that recording
# (headerless S32_LE, 16 kHz, CHANNELS channels) instead; implies offline behaviour.
ODAS_INPUT = env("STT_ODAS_INPUT", "")
RUN_DIR   = env("STT_RUN_DIR", "/run/rsp-stt")           # FIFO, generated cfg, record requests
REC_DIR   = env("STT_REC_DIR", os.path.expanduser("~/recordings"))
# Event stream for other programs (an agent, a display): "host:port" to serve
#   GET /events   Server-Sent Events: speech_start, speech_dir, speech_end, utterance (JSON data)
#   GET /recent   the last utterances as a JSON array
# Off when empty. Bind to 127.0.0.1 unless the LAN should be able to read what is said.
# Speech gate: an utterance is sent to the server only if the Silero VAD model hears at least
# this much continuous speech in it (0 = gate off). The energy VAD and ODAS also fire on
# keyboards, doors and fans, and Whisper writes plausible sentences for those. Needs
# python3-onnxruntime and the model file; without either the gate is off, with a warning.
SPEECH_MIN_MS = env("STT_SPEECH_MIN_MS", 200, int)
SPEECH_MODEL = env("STT_SPEECH_MODEL", "/opt/rsp-stt/silero_vad.onnx")
SPEECH_AUDIT = env("STT_SPEECH_AUDIT", "0") == "1"   # transcribe rejected audio too, and log it
# Experiments: keep every utterance sent to the server as <dir>/<n>.wav plus a line in
# <dir>/index.jsonl with the raw transcript. Off by default; it stores room speech.
SAVE_DIR  = env("STT_SAVE_DIR", "")
HTTP_ADDR = env("STT_HTTP", "")
HTTP_TOKEN = env("STT_HTTP_TOKEN", "")       # if set, clients must send "Authorization: Bearer <token>"

def ignored(az):
    return any(abs((az - c + 180) % 360 - 180) <= w for c, w in ODAS_IGNORE)

FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2
JUNK = ("[BLANK_AUDIO]", "[ Silence ]", "[silence]", "(silence)", "[MUSIC]", "[Music]")

def log(msg):
    sys.stdout.write(msg + "\n"); sys.stdout.flush()

class Loopback:
    """Watches the capture's loopback channels: is the speaker playing, and what.

    The played signal goes to PLAYED, which recognises the board's own voice in an utterance
    later. The idle loopback is not silent but a few steps of noise."""
    HOLD = 10                                  # blocks counted as playing after the speaker stops
    QUIET = 12                                 # the idle loopback is noise of up to 3 of these 16-bit steps

    def __init__(self):
        self.hold = 0
        self.active = False                    # the speaker is playing, or stopped a moment ago

    def see(self, a, keep=True):
        """a: one block, int32 (samples, CHANNELS). Returns the played signal as int16, or
        None while the speaker is idle."""
        ref = a[:, MICS:].sum(axis=1, dtype=np.int64) >> 13
        if np.abs(ref).max() > self.QUIET:
            self.hold = self.HOLD
        elif self.hold > 0:
            self.hold -= 1                     # the room rings on after the speaker stops
        else:
            self.active = False
            PLAYED.add(None)
            return None
        self.active = True
        # the loopback is quiet: 16-bit with 12 dB of gain; the filter adapts to any level
        play = np.ascontiguousarray(np.clip(ref, -32768, 32767).astype(np.int16))
        PLAYED.add(play if keep else None)
        return play

class EchoCanceller:
    """Removes the board's own speaker from the microphone channels of the raw capture.

    The built-in canceller, used when the native one (rsp-aec) is not installed.

    The card's channels after the microphones are a loopback of what the speaker plays. One
    adaptive filter per microphone (libspeexdsp) learns how that reaches the microphone and
    subtracts it. Blocks are passed through untouched while the loopback is idle, so
    normal listening costs nothing and cannot be altered by this."""
    FRAME, TAIL = 160, 1600                    # 10 ms frames, 100 ms of room
    QUIET = Loopback.QUIET

    def __init__(self):
        import ctypes, ctypes.util
        if np is None:
            raise RuntimeError("numpy is needed")
        self.ct = ctypes
        lib = ctypes.CDLL(ctypes.util.find_library("speexdsp") or "libspeexdsp.so.1")
        lib.speex_echo_state_init.restype = ctypes.c_void_p
        lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p] * 4
        lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.lib, self.states = lib, []
        for _ in range(MICS):
            st = lib.speex_echo_state_init(self.FRAME, self.TAIL)
            rate = ctypes.c_int(RATE)
            lib.speex_echo_ctl(st, 24, ctypes.byref(rate))        # SPEEX_ECHO_SET_SAMPLING_RATE
            self.states.append(st)
        self.loopback = Loopback()

    @property
    def active(self):
        return self.loopback.active

    def process(self, raw):
        a = np.frombuffer(raw, dtype="<i4").reshape(-1, CHANNELS)
        whole = len(a) % self.FRAME == 0
        play = self.loopback.see(a, keep=whole)
        if play is None or not whole:
            return raw
        out = a.copy()
        cleaned = np.zeros(self.FRAME, np.int16)
        for c, st in enumerate(self.states):
            mic = np.ascontiguousarray(np.clip(a[:, c] >> 16, -32768, 32767).astype(np.int16))
            echo = np.zeros(len(a), np.int64)
            for i in range(0, len(a), self.FRAME):
                self.lib.speex_echo_cancellation(st, mic[i:i + self.FRAME].ctypes.data,
                                                 play[i:i + self.FRAME].ctypes.data, cleaned.ctypes.data)
                echo[i:i + self.FRAME] = mic[i:i + self.FRAME].astype(np.int64) - cleaned
            # subtract the estimated echo from the full-precision samples
            out[:, c] = np.clip(a[:, c].astype(np.int64) - (echo << 16), -2**31, 2**31 - 1)
        return out.astype("<i4").tobytes()

class PlayedLog:
    """What the speaker played, kept to recognise the board's own voice in an utterance.

    Echo cancellation weakens the speaker's sound in the microphones but leaves some. What
    is left is still the played signal passed through the room, so it is coherent with it,
    frequency by frequency; a person talking is not, even while the speaker plays."""
    BLOCK = RATE * 30 // 1000
    BEHIND, AHEAD = 70, 10                     # utterance vs capture position: up to 2.1 s late, 0.3 s early
    SEG = 4096                                 # 256 ms analysis window, longer than the room's echo

    def __init__(self):
        self.blocks = collections.deque(maxlen=4000)       # two minutes; None = speaker silent
        self.n = 0                                         # blocks seen since the start

    def add(self, samples):
        """samples: int16 array of one 30 ms block, or None when the speaker was silent."""
        self.blocks.append(None if samples is None else np.array(samples, dtype=np.int16))
        self.n += 1

    def similarity(self, pcm, end_s):
        """0..1: the share of an utterance's energy that is the played sound.
        pcm: mono 16-bit utterance; end_s: stream time in seconds at which it ended."""
        if np is None:
            return 0.0
        y = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        m = len(y) // self.BLOCK
        hi = int(round(end_s * 1000 / 30)) + self.AHEAD
        lo = hi - self.AHEAD - m - self.BEHIND
        base = self.n - len(self.blocks)                   # stream position of blocks[0]
        lo, hi = max(lo, base), min(hi, self.n)
        if m < 10 or hi - lo < m:
            return 0.0
        part = [self.blocks[k - base] for k in range(lo, hi)]
        if sum(b is not None for b in part) < 0.2 * m:
            return 0.0                                     # the speaker was silent for nearly all of it
        zero = np.zeros(self.BLOCK, np.int16)
        x = np.concatenate([zero if b is None else b for b in part]).astype(np.float64)
        y = y[:m * self.BLOCK]
        # where in the played sound the utterance fits best
        size = 1 << int(np.ceil(np.log2(len(x) + len(y))))
        cc = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(y, size)), size)[:len(x) - len(y) + 1]
        x = x[int(np.argmax(np.abs(cc))):][:len(y)]
        # magnitude-squared coherence over the speech band, weighted by the utterance's spectrum
        win, hop = np.hanning(self.SEG), self.SEG // 2
        pxx = pyy = pxy = 0
        segs = 0
        for i in range(0, len(y) - self.SEG + 1, hop):
            fx, fy = np.fft.rfft(x[i:i + self.SEG] * win), np.fft.rfft(y[i:i + self.SEG] * win)
            pxx, pyy, pxy = pxx + np.abs(fx) ** 2, pyy + np.abs(fy) ** 2, pxy + fx * np.conj(fy)
            segs += 1
        if segs < 2:
            return 0.0
        band = slice(300 * self.SEG // RATE, 3500 * self.SEG // RATE)
        coh = np.abs(pxy[band]) ** 2 / (pxx[band] * pyy[band] + 1e-9)
        share = float((coh * pyy[band]).sum() / (pyy[band].sum() + 1e-9))
        # unrelated signals still show some coherence when few windows are averaged
        # (about 1/segs, more with overlapping windows): take that chance level off
        return max(0.0, share - 1.5 / segs)

    def overlap(self, nbytes, end_s):
        """0..1: how much of an utterance's time the speaker was playing."""
        m = nbytes // 2 // self.BLOCK
        hi = int(round(end_s * 1000 / 30)) - 10            # the utterance trails the capture a little
        base = self.n - len(self.blocks)
        span = [self.blocks[k - base] for k in range(max(hi - m, base), min(hi, self.n))]
        return sum(b is not None for b in span) / float(m) if m else 0.0

PLAYED = PlayedLog()

def preload_played(path):
    """Replay: take what the speaker played from the recording's own loopback channels, so a
    take is judged as it would have been live."""
    PLAYED.blocks = collections.deque()                    # the whole take, not the last two minutes
    PLAYED.n = 0
    watch, step = Loopback(), PlayedLog.BLOCK * CHANNELS * 4
    with open(path, "rb") as f:
        while True:
            raw = f.read(step)
            if len(raw) < step:
                break
            watch.see(np.frombuffer(raw, dtype="<i4").reshape(-1, CHANNELS))
SPEAKER_AZ = collections.deque(maxlen=8)   # directions of utterances that clearly were the speaker

def from_speaker(az):
    """Is az where the board's own speaker has been heard from?"""
    if not SPEAKER_AZ:
        return False
    mean = math.degrees(math.atan2(sum(math.sin(math.radians(a)) for a in SPEAKER_AZ),
                                   sum(math.cos(math.radians(a)) for a in SPEAKER_AZ)))
    return abs((az - mean + 180) % 360 - 180) <= ECHO_AZ

def wav_bytes(pcm):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    w.writeframes(pcm); w.close()
    return buf.getvalue()

def transcribe(pcm):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in (("response_format", "json"), ("temperature", "0.0"), ("language", LANGUAGE)):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"u.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % boundary).encode())
    parts.append(wav_bytes(pcm))
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(SERVER, data=b"".join(parts), headers={
        "Content-Type": "multipart/form-data; boundary=%s" % boundary})
    resp = urllib.request.urlopen(req, timeout=60)
    return json.loads(resp.read().decode("utf-8", "replace")).get("text", "").strip()

SUBSCRIBERS = []                          # one queue per connected /events client
HISTORY = collections.deque(maxlen=50)    # recent utterance records for /recent

def publish(kind, data):
    """Send an event to every /events client; a client that falls behind is dropped."""
    data = dict(data, event=kind)
    if kind == "utterance":
        HISTORY.append(data)
    for q in list(SUBSCRIBERS):
        try:
            q.put_nowait(data)
        except queue.Full:
            SUBSCRIBERS.remove(q)

class EventHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def do_GET(self):
        if HTTP_TOKEN and self.headers.get("Authorization") != "Bearer " + HTTP_TOKEN:
            self.send_error(401); return
        if self.path == "/recent":
            body = json.dumps(list(HISTORY)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        if self.path != "/events":
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        q = queue.Queue(maxsize=200)
        SUBSCRIBERS.append(q)
        try:
            while q in SUBSCRIBERS:
                try:
                    d = q.get(timeout=15)
                    msg = "event: %s\ndata: %s\n\n" % (d["event"], json.dumps(d))
                except queue.Empty:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode()); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if q in SUBSCRIBERS:
                SUBSCRIBERS.remove(q)

class EventServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def serve_events():
    host, _, port = HTTP_ADDR.rpartition(":")
    srv = EventServer((host or "127.0.0.1", int(port)), EventHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("rsp-stt: events on http://%s:%s/events" % (host or "127.0.0.1", port))

def emit(text, dur, latency, meta):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "text": text,
           "audio_s": round(dur, 2), "stt_s": round(latency, 2)}
    rec.update(meta)
    where = " az=%d" % meta["az"] if "az" in meta else ""
    if "echo" in meta:
        where += " echo=%.2f" % meta["echo"]
    log("[%s] (%.1fs audio, %.2fs stt%s) %s" % (rec["ts"], dur, latency, where, text))
    publish("utterance", rec)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        log("warn: cannot write %s: %s" % (LOG_FILE, e))
    if WEBHOOK:
        try:
            urllib.request.urlopen(urllib.request.Request(
                WEBHOOK, data=json.dumps(rec).encode(),
                headers={"Content-Type": "application/json"}), timeout=10).read()
        except Exception as e:
            log("warn: webhook failed: %s" % e)

# What Whisper writes for noise with no speech in it: sound tags like *sad music* or
# [BLANK_AUDIO], lone punctuation, and a few stock phrases. A transcript that is nothing
# but these is dropped. STT_NOISE_PHRASES overrides the phrase list ("|" separated).
NOISE_PHRASES = set(p.strip().lower() for p in env(
    "STT_NOISE_PHRASES", "thank you.|thanks for watching.|thank you for watching.|you|bye.").split("|"))
TAG = re.compile(r"\*[^*]*\*|\[[^\]]*\]|\([^)]*\)")

def clean(text):
    for j in JUNK:
        text = text.replace(j, "")
    text = text.strip()
    rest = TAG.sub("", text).strip(" .,!?-\n")
    if not rest or text.lower() in NOISE_PHRASES:
        return ""
    return text

RECENT = collections.deque(maxlen=12)   # (start, end, text) of emitted transcripts

def duplicate(t_start, t_end, text):
    """Fallback for same_talker_cut(): a transcript whose time span lies inside one already
    emitted, with mostly the same words, is a second copy of it."""
    words = text.lower().split()
    for s0, e0, other in RECENT:
        if t_start < s0 - 1.0 or t_end > e0 + 1.0:
            continue
        ow = other.lower().split()
        same = sum(m.size for m in difflib.SequenceMatcher(None, words, ow).get_matching_blocks())
        if same >= 0.6 * min(len(words), len(ow)):
            return True
    return False

GATE = []     # [onnxruntime session] once loaded, [None] if unavailable

def speech_session():
    if not GATE:
        GATE.append(None)
        try:
            # Debian's onnxruntime prints hundreds of harmless "Schema error" lines on import
            saved = os.dup(2); null = os.open(os.devnull, os.O_WRONLY); os.dup2(null, 2)
            try:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.intra_op_num_threads = so.inter_op_num_threads = 1
                GATE[0] = ort.InferenceSession(SPEECH_MODEL, so, providers=["CPUExecutionProvider"])
            finally:
                os.dup2(saved, 2); os.close(saved); os.close(null)
        except Exception as e:
            log("warn: speech gate off (%s)" % str(e).splitlines()[0][:120])
    return GATE[0]

def has_speech(pcm):
    """True if Silero finds SPEECH_MIN_MS of continuous speech; stops at the first such run.
    True as well when the gate is off or unavailable."""
    sess = speech_session() if SPEECH_MIN_MS > 0 and np is not None else None
    if sess is None:
        return True
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    # the beamformer output is quiet; bring its peaks to half scale, as the model expects
    peak = float(np.percentile(np.abs(x), 99.9)) if len(x) else 0.0
    x = np.clip(x * min(0.5 / (peak + 1e-9), 30.0), -1.0, 1.0)
    state = np.zeros((2, 1, 128), np.float32); ctx = np.zeros(64, np.float32)
    sr = np.array(16000, np.int64); run = 0; need = max(1, SPEECH_MIN_MS // 32)
    for i in range(0, len(x) - 511, 512):       # the model takes 32 ms chunks + 4 ms context
        chunk = x[i:i + 512]
        prob, state = sess.run(None, {"input": np.concatenate([ctx, chunk])[None, :],
                                      "state": state, "sr": sr})
        ctx = chunk[-64:]
        run = run + 1 if prob[0, 0] > 0.5 else 0
        if run >= need:
            return True
    return False

SAVED = [0]

def save_utterance(pcm, raw, meta):
    try:
        os.makedirs(SAVE_DIR, exist_ok=True)
        SAVED[0] += 1
        name = "%04d.wav" % SAVED[0]
        with open(os.path.join(SAVE_DIR, name), "wb") as f:
            f.write(wav_bytes(pcm))
        with open(os.path.join(SAVE_DIR, "index.jsonl"), "a") as f:
            f.write(json.dumps(dict(meta, file=name, raw=raw)) + "\n")
    except OSError as e:
        log("warn: cannot save utterance: %s" % e)

def handle(frames, meta):
    ab = bool(frames) and isinstance(frames[0], tuple)
    pcm = b"".join(f[0] for f in frames) if ab else b"".join(frames)
    dur = len(pcm) / 2.0 / RATE
    t0 = time.time()
    if AEC and "t" in meta:
        sim = PLAYED.similarity(pcm, meta["t"])
        az = meta.get("az")
        if sim >= ECHO_CLEAR and az is not None:
            SPEAKER_AZ.append(az)                          # this is where the speaker is heard from
        # a distorted voice leaves little that still matches; then direction and timing decide
        there = (az is not None and from_speaker(az) and sim >= 0.03
                 and PLAYED.overlap(len(pcm), meta["t"]) >= 0.6)
        if sim >= ECHO_SIM or there:
            log("drop: own speaker in %.1fs utterance%s (%.0f%% of it is what was played%s)" % (
                dur, " az=%d" % az if az is not None else "", sim * 100,
                ", from the speaker's direction" if sim < ECHO_SIM else ""))
            return
        if sim >= 0.03:
            meta = dict(meta, echo=round(sim, 2))          # kept in the record, to tune the limit
    if not has_speech(pcm):
        said = ""
        if SPEECH_AUDIT:
            try:
                said = " (whisper: %s)" % transcribe(pcm).replace("\n", " / ")[:80]
            except Exception:
                pass
        log("drop: no speech in %.1fs utterance%s%s" % (
            dur, " az=%d" % meta["az"] if "az" in meta else "", said))
        return
    try:
        raw = transcribe(pcm)
        text = clean(raw)
        if SAVE_DIR:
            save_utterance(pcm, raw, meta)
        if ab:
            meta = dict(meta, text_pf=clean(transcribe(b"".join(f[1] for f in frames))))
    except Exception as e:
        log("error: stt request failed (%.1fs utterance dropped): %s" % (dur, e))
        return
    t_end = meta.pop("t_end", t0)
    if "t" in meta:
        t_end = meta["t"]             # position in the audio stream, seconds
    if text and duplicate(t_end - dur, t_end, text):
        log("drop: duplicate of an overlapping source: %s" % text[:60])
        return
    if text:
        RECENT.append((t_end - dur, t_end, text))
        emit(text, dur, time.time() - t0, meta)
    elif DEBUG or dur * 1000 >= LOG_DROP_MS:
        log("drop: %.1fs utterance%s: empty transcript" % (dur, " az=%d" % meta["az"] if "az" in meta else ""))

# Offline replay (STT_OFFLINE=1): the ODAS cfg reads a recording instead of the sound card,
# which runs faster than real time, so queue without limit and drain before exiting.
OFFLINE = env("STT_OFFLINE", "0") == "1" or bool(os.environ.get("STT_ODAS_INPUT"))
PENDING = queue.Queue(maxsize=0 if OFFLINE else 8)   # utterances waiting for the server

def uploader():
    """Send utterances in order, off the capture thread, so recording never stalls."""
    while True:
        handle(*PENDING.get())
        PENDING.task_done()

def submit(pcm, meta=None):
    try:
        PENDING.put_nowait((pcm, dict(meta or {}, t_end=time.time())))
    except queue.Full:
        log("error: stt backlog full (%d waiting), utterance dropped" % PENDING.qsize())

def mono_frame(raw):
    """One 16-bit mono frame from an interleaved multi-channel frame."""
    if np is not None:
        a = np.frombuffer(raw, dtype="<i2").reshape(-1, CHANNELS)
        if MIX != "sum":
            return a[:, int(MIX)].tobytes()
        return (a[:, :MICS].sum(axis=1, dtype=np.int32) // MICS).astype("<i2").tobytes()
    a = array.array("h", raw)
    if MIX != "sum":
        return a[int(MIX)::CHANNELS].tobytes()
    # average the mics in 32-bit so quiet signals keep their precision and nothing clips
    acc = None
    for c in range(MICS):
        ch = audioop.mul(audioop.lin2lin(a[c::CHANNELS].tobytes(), 2, 4), 4, 1.0 / MICS)
        acc = ch if acc is None else audioop.add(acc, ch, 4)
    return audioop.lin2lin(acc, 4, 2)

def rms16(mono):
    if np is not None:
        x = np.frombuffer(mono, dtype="<i2").astype(np.float64)
        return int((x * x).mean() ** 0.5) if len(x) else 0
    return audioop.rms(mono, 2)

class Segmenter:
    """Adaptive energy VAD for one mono stream; submits finished utterances."""
    def __init__(self, min_rms=MIN_RMS, meta=None, name=""):
        self.min_rms, self.meta, self.name = min_rms, meta, name
        self.pre = collections.deque(maxlen=max(1, PRE_MS // FRAME_MS))
        self.recent = collections.deque(maxlen=8)     # recent speech/non-speech flags
        self.floor = float(min_rms) / START_RATIO     # adaptive noise floor (rms)
        self.speaking, self.utt, self.silence_ms, self.n = False, [], 0, 0
        self.seen = []                                # per-utterance snapshots from meta()
        self.levels = []                              # per-frame rms of the utterance
        self.shown = None                             # direction last published for it

    def feed(self, mono, payload=None):
        """mono drives the VAD; payload (default: mono) is what gets sent."""
        if payload is None:
            payload = mono
        rms = rms16(mono)
        loud = rms > max(self.min_rms, self.floor * START_RATIO)
        self.n += 1
        if DEBUG and self.n % 100 == 0:
            log("debug: %s rms=%d floor=%.0f speaking=%s" % (self.name, rms, self.floor, self.speaking))
        if not self.speaking:
            # track the noise floor only while idle: fast down, slow up
            f = self.floor
            self.floor = min(rms, f) * 0.5 + f * 0.5 if rms < f else f * 0.995 + rms * 0.005
            self.recent.append(loud)
            self.pre.append((payload, rms))
            if sum(self.recent) >= 5:
                self.speaking, self.silence_ms = True, 0
                self.utt = [p for p, _ in self.pre]
                self.levels = [r for _, r in self.pre]     # per-frame rms, aligned with utt
                self.recent.clear(); self.seen = []
                m = (self.meta and self.meta()) or {}
                self.shown = m.get("az")               # direction last published, if any
                publish("speech_start", dict(m, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z")))
            return
        self.utt.append(payload)
        self.levels.append(rms)
        self.silence_ms = 0 if loud else self.silence_ms + FRAME_MS
        if loud and self.meta and len(self.utt) % 10 == 0:
            m = self.meta()
            if m:
                self.seen.append(m)
                # tell listeners where the talker is once known, and again when they have moved
                if self.shown is None or abs((m["az"] - self.shown + 180) % 360 - 180) >= DIR_STEP:
                    self.shown = m["az"]
                    publish("speech_dir", {"az": int(round(m["az"])) % 360, "el": int(round(m["el"])),
                                           "src": m["src"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        too_long = len(self.utt) * FRAME_MS >= MAX_S * 1000
        long_enough = len(self.utt) * FRAME_MS >= SOFT_S * 1000
        soft_end = long_enough and self.silence_ms >= SOFT_SIL_MS
        if self.silence_ms >= END_SIL_MS or too_long or soft_end:
            self.speaking = False
            self.pre.clear()
            voiced_ms = len(self.utt) * FRAME_MS - self.silence_ms - PRE_MS
            meta = self.summary()
            meta["t"] = round(self.n * FRAME_MS / 1000.0, 1)
            why = ""
            if self.meta and "az" not in meta:
                why = "untracked"             # ODAS never tracked a source here: noise
            elif "az" in meta and ignored(meta["az"]):
                why = "ignored direction az=%d" % meta["az"]
            lv = sorted(self.levels[:max(1, len(self.levels) - self.silence_ms // FRAME_MS)])
            dyn = 20 * math.log10((lv[int(len(lv) * 0.95)] + 1.0) / (lv[int(len(lv) * 0.25)] + 1.0))
            meta["dyn_db"] = round(dyn, 1)
            if not why and dyn < MIN_DYN_DB:
                why = "flat level (%.1f dB)" % dyn
            if why:
                # short untracked sounds are everyday noise; anything longer must leave a trace
                if DEBUG or why != "untracked" or len(self.utt) * FRAME_MS >= LOG_DROP_MS:
                    log("drop: %s %.1fs %s dyn=%.1f" % (self.name, len(self.utt) * FRAME_MS / 1000.0, why, dyn))
                voiced_ms = 0
            publish("speech_end", dict(meta, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                       sent=voiced_ms >= MIN_MS))
            if voiced_ms >= MIN_MS:
                submit(self.utt, meta)
            elif DEBUG:
                log("debug: %s dropped %dms blip" % (self.name, voiced_ms))
            self.utt = []

    def summary(self):
        """Direction of the utterance: circular mean of the azimuths seen while speaking."""
        if not self.seen:
            return {}
        sx = sum(math.cos(math.radians(m["az"])) for m in self.seen)
        sy = sum(math.sin(math.radians(m["az"])) for m in self.seen)
        ids = [m["src"] for m in self.seen]
        return {"az": int(round(math.degrees(math.atan2(sy, sx)))) % 360,
                "el": int(round(sum(m["el"] for m in self.seen) / len(self.seen))),
                "src": max(set(ids), key=ids.count)}

def read_exact(f, n):
    buf = b""
    while len(buf) < n:
        d = f.read(n - len(buf)) if hasattr(f, "read") else f.recv(n - len(buf))
        if not d:
            return b""
        buf += d
    return buf

def run_arecord():
    cmd = ["arecord", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", str(RATE),
           "-c", str(CHANNELS), "-t", "raw"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    log("rsp-stt: capturing %s channel=%s of %d -> %s" % (DEVICE, MIX, CHANNELS, SERVER))
    seg = Segmenter()
    while True:
        raw = read_exact(proc.stdout, FRAME_BYTES)
        if not raw:
            log("error: arecord ended (exit %s)" % proc.poll())
            return 1
        seg.feed(mono_frame(raw))

TRACKS = [None] * 8     # latest tracked source per ODAS slot: {"src", "az", "el"} or None

def track_reader(conn):
    """Parse the stream of JSON objects ODAS writes to its "tracked" sink."""
    dec, buf = json.JSONDecoder(), ""
    while True:
        d = conn.recv(65536)
        if not d:
            return
        buf += d.decode("utf-8", "replace")
        while True:
            buf = buf.lstrip()
            try:
                obj, end = dec.raw_decode(buf)
            except ValueError:
                break
            buf = buf[end:]
            for i, src in enumerate(obj.get("src", [])[:len(TRACKS)]):
                if src.get("id", 0) and src.get("activity", 0) > 0.5:
                    x, y, z = src["x"], src["y"], src["z"]
                    TRACKS[i] = {"src": src["id"],
                                 "az": round(math.degrees(math.atan2(y, x)) % 360, 1),
                                 "el": round(math.degrees(math.atan2(z, math.hypot(x, y))), 1)}
                else:
                    TRACKS[i] = None

def listen(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port)); s.listen(1)
    return s

def odas_config(input_path):
    """Write the ODAS config actually used: the template with its sound-card input replaced
    by input_path (FIFO or recording) and the sink ports taken from the environment."""
    c = open(ODAS_CFG).read()
    c, n = re.subn(r'type = "soundcard";\s*card = \d+;\s*device = \d+;',
                   'type = "file"; path = "%s";' % input_path, c)
    if n != 1:
        raise SystemExit("error: no soundcard input found in %s" % ODAS_CFG)
    for std, port in ((9000, ODAS_TRACK_PORT), (9002, ODAS_AUDIO_PORT), (9003, ODAS_SEP_PORT)):
        c = c.replace("port = %d;" % std, "port = %d;" % port)
    path = os.path.join(RUN_DIR, "odas-%d.cfg" % os.getpid())
    with open(path, "w") as f:
        f.write(c)
    return path

def capture_to(fifo):
    """Own the sound card: arecord -> FIFO for ODAS, and -> a file while a recording is
    requested. A request is a file RUN_DIR/record.req holding "<seconds> <label>"; the
    recording is REC_DIR/<UTC time>-<label>.raw and RUN_DIR/record.active exists meanwhile."""
    block = RATE * 30 // 1000 * CHANNELS * 4                 # 30 ms of S32 frames
    cmd = ["arecord", "-q", "-D", DEVICE, "-f", "S32_LE", "-r", str(RATE),
           "-c", str(CHANNELS), "-t", "raw", "--buffer-time=500000"]
    out = open(fifo, "wb", buffering=0)                       # blocks until odaslive opens it
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)      # only now, or the card overruns
    req, active = os.path.join(RUN_DIR, "record.req"), os.path.join(RUN_DIR, "record.active")
    rec, left, n = None, 0, 0
    aec = helper = loopback = None
    if AEC and os.access(AEC_BIN, os.X_OK) and np is not None:
        # the native canceller sits between this process and ODAS; recordings keep the raw capture
        helper = subprocess.Popen([AEC_BIN] + AEC_ARGS.split(), stdin=subprocess.PIPE, stdout=out)
        loopback = Loopback()
        log("rsp-stt: echo cancellation on (%s %s, loopback channels %d-%d)"
            % (os.path.basename(AEC_BIN), AEC_ARGS or "aec3", MICS + 1, CHANNELS))
    elif AEC:
        try:
            aec = EchoCanceller()
            log("rsp-stt: echo cancellation on (built-in, loopback channels %d-%d)" % (MICS + 1, CHANNELS))
        except Exception as e:
            log("warn: echo cancellation unavailable: %s" % e)

    def leave(why):
        log("error: %s" % why)
        if helper is not None:
            try:
                helper.stdin.close()                          # let it pass on what it still holds
                helper.wait(timeout=2)
            except Exception:
                pass
        os._exit(1)                                           # let systemd restart everything
    try:
        os.unlink(active)
    except OSError:
        pass
    while True:
        raw = read_exact(proc.stdout, block)
        if not raw:
            leave("arecord ended (exit %s)" % proc.poll())
        if helper is not None:
            try:
                helper.stdin.write(raw)
                helper.stdin.flush()
            except (BrokenPipeError, OSError):
                leave("rsp-aec ended (exit %s)" % helper.poll())
            loopback.see(np.frombuffer(raw, dtype="<i4").reshape(-1, CHANNELS))
            if loopback.active:
                PLAYBACK[0] = time.time()
        elif aec is not None:
            out.write(aec.process(raw))                       # recordings below keep the raw capture
            if aec.active:
                PLAYBACK[0] = time.time()
        else:
            out.write(raw)
        n += 1
        if rec is None and n % 10 == 0 and os.path.exists(req):
            try:
                words = open(req).read().split()
                os.unlink(req)
                secs = float(words[0])
                label = re.sub(r"[^A-Za-z0-9_.-]", "_", words[1]) if len(words) > 1 else "take"
                if not os.path.isdir(REC_DIR):
                    os.makedirs(REC_DIR)
                name = os.path.join(REC_DIR, "%s-%s.raw" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), label))
                rec, left = open(name, "wb"), int(secs * 1000 / 30)
                open(active, "w").write(name + "\n")
                log("record: %.0fs -> %s" % (secs, name))
            except Exception as e:
                log("warn: bad record request: %s" % e)
        if rec is not None:
            rec.write(raw)
            left -= 1
            if left <= 0:
                rec.close(); rec = None
                os.unlink(active)
                log("record: done")

def odas_command(cfg):
    return [ODAS_BIN] + ODAS_ARGS.split() + ["-c", cfg]

def run_odas():
    """odaslive connects out to its sinks, so listen first, then start it."""
    if not os.path.isdir(RUN_DIR):
        os.makedirs(RUN_DIR)
    if ODAS_INPUT:
        source = os.path.realpath(ODAS_INPUT)
        if AEC and np is not None:
            preload_played(source)
    else:
        source = os.path.join(RUN_DIR, "raw.fifo")
        if os.path.exists(source):
            os.unlink(source)
        os.mkfifo(source)
    cfg = odas_config(source)
    audio_srv, track_srv = listen(ODAS_AUDIO_PORT), listen(ODAS_TRACK_PORT)
    sep_srv = listen(ODAS_SEP_PORT)
    # ODAS reports fatal errors on stdout; keep them (they land in the journal)
    proc = subprocess.Popen(odas_command(cfg), stdout=sys.stderr)
    if not ODAS_INPUT:
        threading.Thread(target=capture_to, args=(source,), daemon=True).start()
    for srv in (audio_srv, track_srv, sep_srv):
        srv.settimeout(20)
    try:
        track, _ = track_srv.accept()
        sep, _ = sep_srv.accept()
        audio, _ = audio_srv.accept()
    except socket.timeout:
        log("error: odaslive did not connect (exit %s)" % proc.poll())
        proc.kill()
        return 1
    threading.Thread(target=track_reader, args=(track,), daemon=True).start()
    log("rsp-stt: ODAS %d source streams, sending %s (%s) -> %s"
        % (ODAS_STREAMS, ODAS_SEND, ODAS_CFG, SERVER))
    segs = [Segmenter(ODAS_MIN_RMS, (lambda i=i: TRACKS[i]), "odas%d" % i)
            for i in range(ODAS_STREAMS)]
    cur, lead = [0], [0]                       # dominant mode: current slot, frames a rival has led
    one = Segmenter(ODAS_MIN_RMS, (lambda: TRACKS[cur[0]]), "odas")
    nbytes = FRAME_SAMPLES * ODAS_STREAMS * 2
    while True:
        raw = read_exact(audio, nbytes)
        if not raw:
            if OFFLINE:
                PENDING.join()
                return 0
            log("error: odaslive audio stream ended (exit %s)" % proc.poll())
            proc.kill()
            return 1
        raw_sep = read_exact(sep, nbytes)     # same hop clock as the postfiltered stream
        if not raw_sep:
            log("error: odaslive separated stream ended (exit %s)" % proc.poll())
            proc.kill()
            return 1
        a = np.frombuffer(raw, dtype="<i2").reshape(-1, ODAS_STREAMS)
        b = np.frombuffer(raw_sep, dtype="<i2").reshape(-1, ODAS_STREAMS)
        if ODAS_MODE == "dominant":
            # The postfiltered streams share the energy out between slots, so the loudest
            # one is where the talker currently is. Follow it, with hysteresis so the
            # payload does not hop between beams on every syllable.
            lv = np.sqrt((a.astype(np.float64) ** 2).mean(axis=0))
            best = int(lv.argmax())
            if best != cur[0] and lv[best] > 2.0 * lv[cur[0]] + 30:
                lead[0] += 1
                if lead[0] >= 240 // FRAME_MS:
                    cur[0], lead[0] = best, 0
            else:
                lead[0] = 0
            i = cur[0]
            pf, sp = a[:, i].tobytes(), b[:, i].tobytes()
            one.feed(a[:, best].tobytes(), {"separated": sp, "both": (sp, pf)}.get(ODAS_SEND, pf))
            continue
        for i, seg in enumerate(segs):
            pf, sp = a[:, i].tobytes(), b[:, i].tobytes()
            seg.feed(pf, {"separated": sp, "both": (sp, pf)}.get(ODAS_SEND, pf))

def main():
    d = os.path.dirname(LOG_FILE)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    threading.Thread(target=uploader, daemon=True).start()
    if HTTP_ADDR and not OFFLINE:
        serve_events()
    if SOURCE == "odas":
        if np is None:
            log("error: STT_SOURCE=odas needs numpy")
            return 1
        return run_odas()
    return run_arecord()

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
