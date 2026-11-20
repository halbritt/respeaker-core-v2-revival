# respeaker-core-v2-revival

Mainline Linux, beamforming and speech-to-text for the discontinued Seeed ReSpeaker Core v2.

A current operating system and audio stack for the Seeed ReSpeaker Core v2 (RK3229,
six-microphone ring, 2x AC108 ADC), whose vendor image is Debian 9 with a 4.4 kernel from 2018.

What is here:

- **OS**: Debian 13 (trixie) with a mainline 6.18 kernel, built with the Armbian framework,
  booted from an SD card by the bootloader already on the board's eMMC.
- **Drivers**: a device tree for the board and a port of Seeed's AC108 codec driver to the
  current ASoC API (8-channel capture: 6 mics + 2 playback-loopback channels).
- **Audio pipeline**: [ODAS](https://github.com/introlab/odas) localises, tracks and beamforms
  talkers; `rsp-stt` cuts the result into utterances and sends them to a
  [whisper.cpp](https://github.com/ggml-org/whisper.cpp) server on another machine.
- **Interfaces**: transcripts with direction as JSON lines, an HTTP event stream, and a small
  listener that passes addressed speech to a command (for example an agent).
- **Tools**: raw recording and offline replay for repeatable experiments, an armhf cross
  toolchain, and the bring-up kit used to get the kernel booting without a serial console.

Status: works on one board. Not yet rebuilt from scratch end to end; see "Known gaps".

## Layout

| Path | Contents |
|---|---|
| `board/` | Runtime: `rsp-stt.py`, its settings and unit, the ODAS config, `rsp-aec.cc` (echo canceller), `rsp-record`, `rsp-replay`, `rsp-listen`, `rsp-leds` |
| `newos/port/` | Board device tree, kernel patches (`0001` AC108 driver port, `0002` output gain control for the SoC codec), reference sources |
| `newos/mk-sd-image.sh` | Assembles the SD image from an Armbian rootfs plus our kernel, dtb and services |
| `newos/overlay/`, `newos/debug-initrd/` | Boot safety net and bring-up tools |
| `newos/cross/` | Docker cross toolchain (Debian 13 armhf), the ODAS build with patches, and the `rsp-aec` build |
| `newos/vendor-4.4/` | Vendor kernel config and Seeed's kernel patches, for reference |
| `tools/bin/rsp-serial` | Run a command on the vendor image's USB serial console |
| `tests/` | Host test suite: `mise run test`, or `python3 -m pytest tests` (needs pytest and numpy; some tests skip without `dtc`, a static busybox or the upstream sources) |

Not in the repository (downloaded or generated): the Armbian build tree, Seeed's image, the
Rockchip loader tools, kernel/rootfs output, a static busybox for the initramfs.

## Audio pipeline

```
arecord (8 ch, 16 kHz, S32) --> rsp-aec --FIFO--> odaslive --+-- tracked sources (JSON, TCP 9000)
        |              (hum and own speaker removed)         +-- postfiltered audio (TCP 9002)
        +--> ~/recordings/*.raw (on request)                 +-- separated audio    (TCP 9003)
                                                        |
                              rsp-stt: follow the loudest source, VAD, utterances
                                                        |
                                    whisper server  -->  transcripts.jsonl, /events
```

- `rsp-stt` owns the sound card and feeds ODAS through a FIFO, so it can save the raw capture
  while transcription carries on, and a capture overrun does not end the pipeline.
- On the way, `rsp-aec` high-passes the microphones at 120 Hz and removes the board's own
  speaker (see "Not hearing itself"). The filter is there because ODAS localises steady
  low-frequency sound very well: with brown noise playing in the room from another speaker
  (a sleep sound; at the microphones it was strongest at 60-140 Hz, and is called "the hum"
  below) it tracked that speaker as a talker and produced a 12-second non-speech "utterance" about
  four times a minute, each lighting the LED ring and costing a pass of the speech gate.
  With the filter there were none in ten minutes with the same hum. `STT_AEC_ARGS="--highpass 0"`
  turns it off. Recorded speech with that hum added, replayed five times at different
  offsets: 19% word error rate with the filter, 23% without. With the hum doubled it was the
  other way round (42% against 26%): unfiltered, the hum holds the detector open, so
  everything is sent; filtered, quiet stretches of speech fall below the level detector.
- The level detector's threshold for the ODAS stream is 40 (`STT_ODAS_MIN_RMS`); the
  plain-microphone value of 150 was too high for ODAS's noise-masked output. On the same
  speech-with-hum test the error rate fell from about 19% to about 11%, clean speech stayed
  the same, and six minutes of fan noise produced no transcript at either setting (29 sounds
  reached the speech gate instead of 17).
- A sound that ODAS never tracked is dropped as noise if it is shorter than 2 s
  (`STT_UNTRACKED_MIN_MS`); a longer one goes to the speech gate. In a noisy room ODAS loses
  a talker for seconds at a time, and those seconds used to be discarded unheard. Their
  transcripts carry `"untracked": true` and no direction.
- The postfiltered stream (noise-masked) decides when someone is speaking; the separated
  stream (plain beamformer) is what is sent to Whisper, which recognises it slightly better.
- `STT_ODAS_MODE=dominant` (the program's default) produces one stream that follows the
  loudest tracked source, so a talker who moves, or whose reflection is tracked as a second
  source, stays one utterance. `separate` segments each tracked source on its own.
- `STT_ODAS_MODE=mic` (set in `rsp-stt.env`) detects speech on, and sends, one microphone
  after the echo canceller (`STT_MIC_CHANNEL`, 0). ODAS then only supplies the direction.
  Measured on a 459-word reading with the board talking over part of it, replayed through
  both modes: 9.6% word error rate in `mic` mode against 39% in `dominant` mode, where runs
  of 17 to 36 words were never sent because ODAS had lost the talker or its filtered stream
  had gone quiet. On an older take recorded with clipping microphones the two modes were
  equal (50% and 52%). At a few metres one microphone is recognised as well as a beam from
  this array (5 to 6% for both, cut by hand); the beam is not what was missing.
  - In this mode an utterance is cut where the board's speaker starts and where it has
    stopped, so that the board's voice and a person are judged separately.
  - While the board speaks, a sound counts as speech only where the canceller's output is
    within 9 dB of its input (`STT_BESIDE_DB`). What the canceller leaves of the board's own
    voice no longer resembles what was played, so the own-voice check misses it, but it is
    13 dB or more below the microphone before the canceller; a person is in both alike.
    Replayed with and without this rule: the 459-word reading 7.2% against 9.6% (a board
    prompt that had come through as 17 garbled words is gone, four words of the reader are
    lost), the older clipped take 17% and 27% in two replays against 50%, and two recordings of the board reading
    aloud in an empty room no transcript at all against one and two wrong ones.
  - Over the board's voice a person is heard in pieces, so an utterance there ends only
    after 1.5 s without them (`STT_PLAY_END_SILENCE_MS`).
  - After a start the canceller knows nothing of the speaker, and the first playback put
    three wrong words in the transcript. Until it has heard 3 s of playback
    (`STT_BESIDE_WARM_MS`), everything heard while the board speaks is taken for the board.
  - Limits: the rule needs the speaker to stay within its clean range. At +4.8 dB of playback
    gain a linear filter still removed 19 to 21 dB of the speaker; at 0 dB it removed all of
    it down to the room's noise. With someone moving beside the board while it read aloud,
    parts of the board's speech were still transcribed.
- Utterances end after 1.5 s of silence, or at the next short pause once they pass 12 s, with
  a hard cap of 25 s.
- Before an utterance is sent, a speech gate ([Silero VAD](https://github.com/snakers4/silero-vad)
  through onnxruntime) checks that it contains at least 200 ms of continuous speech. Keyboards,
  doors and fans trigger the energy detector and ODAS, and Whisper writes plausible sentences
  for them. The gate runs only on finished utterances and stops at the first speech it finds,
  so it adds about 0.1-0.3 s to speech and nothing to the capture path.
- Transcripts that are only Whisper's noise artefacts (`*music*`, lone punctuation, a short
  list of stock phrases) are dropped; `STT_ODAS_IGNORE` drops whole directions.
- `STT_SOURCE=arecord` bypasses ODAS and averages the six mics (works on the vendor image).

Each transcript is one JSON object: `ts`, `text`, `audio_s`, `stt_s`, and with ODAS `az`
(azimuth), `el` (elevation), `src` (ODAS track id), `t` (seconds since start), `dyn_db` (level
variation), and `echo` or `untracked` where they apply.

Settings live in `/etc/default/rsp-stt` (`board/rsp-stt.env`); site-specific values such as
the Whisper server address go in `/etc/default/rsp-stt.local`.

### Geometry

Mics up. Azimuth 0 is the middle of the USB-A side and increases counter-clockwise seen from
above: HDMI side 60, micro-USB side 180, microSD side 300. The mics are at the corners of the
hexagon; the silkscreen's Mic 1..6 are capture channels 0..5, at 30, 330, 270, 210, 150 and 90
degrees. Measured accuracy with a talker 1-2 m away: within about 20 degrees.

### Recording and replay

```
rsp-record 120 walk-test           # save the next 120 s of raw capture; second blue LED is lit
rsp-replay ~/recordings/<file>.raw # run it through ODAS + Whisper offline, faster than real time
```

Recordings are headerless S32_LE, 16 kHz, 8 channels (about 61 MB per minute), kept under
`~/recordings` with a 2 GB cap. Replay uses the production settings; any `STT_*` variable in
the environment overrides them, so one take can be compared across settings or ODAS builds.

### Connecting an agent

Set `STT_HTTP=127.0.0.1:8911` (see `board/rsp-stt.env`). `GET /events` is a Server-Sent Events
stream of `speech_start`, `speech_dir` (the talker's direction, once known and when it moves),
`speech_end` and `utterance`; `GET /recent` returns the last
utterances. It carries what is said in the room, so it binds to localhost by default; forward
it with `ssh -L 8911:127.0.0.1:8911 <board>`, or bind it wider and set `STT_HTTP_TOKEN`.

`rsp-listen` (standard library only, runs anywhere) consumes the stream and decides which
speech is meant for the agent:

```
rsp-listen --print
rsp-listen --name computer --strip-wake -- ./my-agent   # utterance JSON on stdin, text in $RSP_TEXT
```

- `--name` matches a name anywhere in the sentence ("could you check that, computer?") and
  tolerates the transcriber's spelling of it. `--wake` takes a regular expression instead.
- After an addressed utterance the conversation stays open for `--follow` seconds (default
  20), counted from when the command returns, so follow-ups need no name.
- Follow-ups must come from within `--follow-az` degrees (default 40) of the talker, so a
  television or a second conversation elsewhere in the room is not picked up. The reference
  direction moves with the talker.
- The command gets `addressed` (`name`, `wake` or `follow`) in the JSON and `$RSP_WHY`.

Matching is done on the transcript, so it needs no model on the board, but everything said
in the room is still transcribed first.

### LED ring

`rsp-leds` (service `rsp-leds`) reads the same event stream and drives the twelve APA102
LEDs over SPI:

| State | Ring |
|---|---|
| Someone is speaking | Blue pointer towards the talker, as soon as the direction is known; it follows them if they move |
| Utterance being transcribed | Amber pointer, for at most 8 s |
| Transcript arrived | Green pointer for 0.6 s |
| Otherwise | Dark, also when the speech service is down |

The ring's supply is switched by GPIO2_A2 (low = on), held by the `rsp-led-power` service.
LED 0 is at Mic 2 (azimuth 330) and the numbers run clockwise seen from above.

### Speaker

The speaker connector (JST PH 2.0) and the 3.5 mm jack are driven by the SoC's own codec,
which shows up as playback device 1 of the same sound card. `/etc/asound.conf` makes it the
default output. Two volume controls: `Master` is the codec's analogue output stage (kernel
patch `0002`; the Armbian driver alone fixes it at step 24), `Speaker` is a software gain:

```
aplay sound.wav                       # any rate or format
amixer sset Master 26                 # codec output stage: 0..31, 26 = 0 dB, 31 = about +6 dB, default 24
amixer sset Speaker -- 0dB            # software gain on top: -40 to +12 dB, clips above 0 dB
sudo alsactl store                    # keep both across reboots
```

The codec shares its I2S controller with the microphones, so while they are recording,
playback runs at 16 kHz and ALSA converts. The codec driver is an Armbian kernel patch, not
mainline. Tested by playing a tone and finding it in the microphone capture, which carried on
without a gap.

### Not hearing itself

The sound card's last two channels are a loopback of what the speaker plays. Two stages use
them (`STT_AEC=0` turns both off):

1. **Cancellation.** `rsp-aec` sits between the capture and ODAS and subtracts the speaker
   from the six microphone channels with the Speex echo canceller, one adaptive filter per
   microphone. It takes off about 13 dB and leaves a person's voice as it was, so ODAS can
   still find them. While the loopback is idle it computes nothing and passes the
   microphones on unchanged (delayed by 4 ms, see below). `STT_AEC_ARGS="--engine aec3"`
   selects WebRTC's AEC3, which removes the speaker almost completely and a quieter
   talker with it. Without `/opt/rsp-stt/rsp-aec` the service falls back to a built-in
   canceller (libspeexdsp through ctypes).
2. **Recognising the leftover.** What is left of the speaker is still intelligible, so an
   utterance is dropped as the board's own voice (`drop: own speaker` in the log) when
   - it has no direction or comes from the speaker's direction (`STT_SPEAKER_AZ`, 185
     degrees on this board, within `STT_ECHO_AZ`, default 25), and a tenth of its energy is
     coherent with what was played (`STT_ECHO_SHARE`), or it overlaps playback at all;
   - it comes from anywhere else and its words are the board's. No limit on the sound
     separates the two cases there (a person reading clearly over the board scored up to
     67% played sound, the board alone as little as 49%), so the service also sends Whisper
     what the speaker played during those seconds, takes runs of three or more of those
     words out of the utterance's transcript, and keeps what is left. With half or more
     played sound (`STT_ECHO_SHARE_ELSEWHERE`) at least eight words must be left
     (`STT_ECHO_WORDS`). Such transcripts carry `over_speaker` and `own_words`.
3. **Following the right beam.** The service follows the loudest ODAS beam. While the board
   speaks that is its own, so when something else is tracked as well, the speaker's beam
   is left out of the choice.

**Talking over the board, measured with a person** (2026-11-18): the board read a 70-second
text at its usual volume while someone across the room read poems aloud over it, three
times. At the microphones the speaker was 25 dB louder than the person.

| | What reached a transcript while the board spoke |
|---|---|
| AEC3, one 10% limit (first take, live) | nothing |
| Speex, 50% limit by direction (second take, live) | nothing: the service followed the speaker's beam |
| the same, following the person's beam (third take, live) | three of four passages, the first two mixed with the board's words |
| the same with the word comparison (all three takes, replayed) | every passage; 23 of the board's words removed from one transcript |

The last row has not been run live. On a take of the board speaking alone for 85 s the same
settings let one word through ("So"). Words the board said that Whisper heard differently
stay in the person's transcript ("Chandlery", "the fish farmer"). One person, one room.

The earlier comparison below was made by adding recordings together, and it pointed the
other way for a quiet talker: it did not show that AEC3's suppressor leaves ODAS nothing to
steer to.

Measured on the board (2026-11-18) with a recording of the board reading a poem aloud at a
loud setting, and a recording of a person reading in the same room, replayed through ODAS and Whisper.
The two were also added together at different levels to test talking over the speaker:

| | built-in (speex) | `rsp-aec` (AEC3) |
|---|---|---|
| Speaker's level in the microphones | -10 dB | -16 dB (-30 dB with `--gentle 1`) |
| Speaker alone: its words reaching Whisper, of 96 | 94, all then caught by stage 2 | 0 |
| Person as loud as the speaker: word error rate | 23% | 38% |
| Person 7 dB quieter than the speaker | 81% | 56% |
| Person 17 dB quieter (the recording as made) | not usable | not usable |
| CPU while the speaker plays | about 70% of a core (in Python) | about 55% |
| CPU while it is silent | none | under 1% |

Speex inside `rsp-aec` costs about 16% of a core while the speaker plays (Debian's library
in the same program: 27%). What was tried, on a 110 s take with 70 s of playback, six
microphones, same cancellation (13 dB) in every case:

| Build of the Speex canceller | CPU seconds |
|---|---|
| Debian's libspeexdsp, or the source at `-O2` | 19.9-20.2 |
| `-O3 -ffast-math` (the compiler vectorises the filter loops with NEON) | 14.7 |
| the same with 16 ms blocks (a 512-point transform in place of 320) | 12.3 |
| the same with hand-written NEON for the four filter loops | 12.1 |
| FFTW (NEON) in place of Speex's own FFT, 10 ms / 16 ms blocks | 14.2 / 12.5 |
| fixed-point build | 20.2 |

The third row is what ships. FFTW as built into Speex also plans with `FFTW_PATIENT`, which
took three minutes at start-up on this board. Speex's `--enable-neon` only covers its
resampler.

Things that matter when changing any of this:

- **The speaker limits what any canceller can do.** A least-squares filter fitted offline to
  that recording removes only 12 dB: the rest of what the microphones hear is not a filtered
  copy of the loopback but distortion of the small open speaker at that volume. The linear
  filters (speex, or AEC3 with `--linear`) are at that limit. A cleaner speaker, a lower
  volume or more distance between speaker and microphones will do more than tuning.
- **AEC3 needs the echo to arrive clearly after the loopback.** Here it arrives 1.6 ms after,
  and AEC3's filters then remove 2 dB instead of 10. `rsp-aec` therefore holds the
  microphones back by 4 ms.
- **AEC3's stock suppressor is tuned for a talker louder than the echo** and removes a quiet
  talker with it. `--gentle N` lets N times more echo through before it suppresses; 16 is the
  default (at 64 the speaker's own words started to come through).
- **Debian's `libwebrtc-audio-processing` is built without NEON** and does not install the
  AEC3 headers, and its linear-output mode crashes. `newos/cross/build-aec.sh` builds the
  library (1.3) from source with NEON and links it in; that took one six-channel instance
  from 59% to 41% of a core. Six single-channel instances cost 159%.
- libspeexdsp's NEON option covers its resampler only, not the echo canceller.

`rsp-replay` treats a recording as the live capture: it feeds it to ODAS through `rsp-aec`
and reads what the speaker played from the recording's own loopback channels.

## ODAS

Built from introlab/odas commit 004cb82 plus `newos/cross/patches/`, which
`newos/cross/build-odas.sh` applies in order:

| Patch | What it fixes | State |
|---|---|---|
| `0001` | `steer2demixing_ds_process` wrote through a stale index when every channel was masked for a track (a source near the board's plane), which crashed `odaslive`. Same fix as upstream PR #309. | Running on the board |
| `0002` | ALSA capture: short reads counted as full, and an overrun ended the stream. | Reviewed only: `rsp-stt` feeds ODAS from a FIFO, so this path is not used here |
| `0003`-`0008` | Undefined float-to-index conversions, a stale index in the scan, an uninitialised accumulator, a process-wide FFTW cleanup, overlapping `sprintf`. | Built and replayed: same utterance counts on five recorded takes; not yet the build on the board |

`board/odas-respeaker-core-v2.cfg` also sets each mic's angle to 180 degrees so low sources
are not masked. Build with `newos/cross/cross.sh ./build-odas.sh` and install the resulting
`bin/` and `lib/` under `/opt/odas`. Compiler tuning for the Cortex-A7 made no measurable
difference. `rsp-stt` starts `odaslive -s` (one thread): its own threads spent a third of
their CPU time waking each other. On a ten-minute take that gave the same transcripts for 42%
less CPU; live it uses about 50% of one core where the threaded one used 80-100%
(`STT_ODAS_ARGS=` brings the threads back).

## OS image

`newos/mk-sd-image.sh` lays an Armbian trixie rootfs and our kernel out the way Seeed's
bootloader expects (`/boot/uEnv.txt`, kernel, dtb and initrd on ext4 partition 2). With the
card inserted the board boots it; without it, the system on the eMMC boots.

```
# in newos/armbian-build (Armbian framework; copy newos/port/000*.patch to
# userpatches/kernel/archive/rockchip-6.18/ first):
./compile.sh build BOARD=rk322x-box BRANCH=current RELEASE=trixie BUILD_MINIMAL=yes KERNEL_CONFIGURE=no
sudo WIFI_SSID=<network> ./mk-sd-image.sh --armed                 # bring-up safety net (default)
sudo WIFI_SSID=<network> ./mk-sd-image.sh --armed --release       # release profile
```

Options:

- `--armed`: writes `/boot/uEnv.txt` so the board boots the card on reset (default writes
  `/boot/uEnv.txt.disabled`).
- `--safety=bringup|release` (or `--bringup` / `--release`, or `SAFETY_PROFILE` env; default
  `bringup`): selects the boot safety net profile, written to `/etc/rsp-safety`; `release`
  is also added to the kernel command line in `uEnv.txt`. See the warning below before
  choosing `release`.

Two things that cost the most time, for anyone porting to a similar board:

- **USB PHY probe deferral**: giving the USB PHY ports a `phy-supply` made
  `rockchip_usb2phy_probe()` defer after it had queued work, which then ran on freed memory
  about 4 s into boot. The board dts leaves the property out.
- **Wifi**: the AP6212's firmware cannot do 802.11w; a supplicant config that offers it
  (netplan's does) associates and is dropped. The image uses a plain WPA2 config.

### Boot safety net

The board was brought up without a serial console, so a failed boot has to recover by
itself. Two profiles exist, chosen by `rsp_safety=bringup|release` on the kernel command
line, else the first line of `/etc/rsp-safety`, else `bringup`. An unrecognised value on
the command line means `bringup`.

- **Bring-up (default).** This is the profile the board runs and the only one that has
  booted on hardware.
  - The initramfs (`newos/debug-initrd/init`) mounts the card, reads `/sys/fs/pstore`,
    then loads `dw_wdt.ko` and arms the hardware watchdog. If the previous boot panicked
    or hung, it saves that kernel log to `/boot-debug/`, renames `uEnv.txt` so the
    bootloader falls back to the eMMC system, and resets. `rspkeep` on the command line
    suppresses the fallback.
  - `/sbin/rsp-preinit` runs a dead-man switch: if `rsp-keep.service` has not seen a wifi
    address and a running sshd within 300 s (`rsp_deadman=`), it saves the journal,
    renames `uEnv.txt` and resets.
- **Release (`--release`).** For a card that should keep booting itself after a power cut
  or a long wifi outage. **It has no automatic fallback**: a kernel that panics or hangs
  on every boot will loop, and a board whose wifi never comes up stays unreachable until
  the card is pulled. Do not use it on a board without a serial console or other way in.
  It has not been booted on hardware. Crash logs are still saved (the five most recent are
  kept), the dead-man switch is off, the initramfs does not load the USB serial gadget
  (`rsp_gserial=1` turns it back on), and the watchdog is handed from the initramfs to
  systemd (`RuntimeWatchdogSec=15s`) by stopping the feeder process.
- `newos/debug-initrd/` also has the staged device-tree test runner (`run-stage.sh`), a
  USB-serial shell for the initramfs, and live device-tree overlays used to bisect the boot
  crash. A boot without `rspboot` on the command line is a staged test: when no stage is
  left the initramfs disarms the card and returns to the eMMC system, in either profile.

## Known gaps

- A successor to this audio pipeline, with the microphone array as configuration and without
  ODAS, is planned as a separate project: [earshot](https://github.com/halbritt/earshot).
- The image built by `mk-sd-image.sh` was flashed and cold-booted once (2026-11-17); images
  built after later changes were not booted again.
- The new OS runs from the SD card. The eMMC works under 6.18 (clock limited to 37.5 MHz),
  but installing to it is not scripted.
- A local wake-word model is not set up.
- Speech in loud steady noise: with noise in the speech band within about 5 dB of the
  talker, a fifth to a third of the words were still lost (see the hum measurements under "Audio
  pipeline"). All of this was measured with one talker and one noise source.
- Talking over the board while it speaks (barge-in): see "Not hearing itself". The final
  rule was checked by replaying three takes of one person, not yet live, and words of the
  board's that Whisper mishears stay in the person's transcript. The microphones peak at
  0.90-0.95 of full scale while the board speaks at its usual volume.
- The kernel's USB PHY use-after-free (see above) is worked around in the device tree; a fix
  for the driver is in `newos/port/patches/` and has not been sent upstream.
- CPU frequency: Armbian's `ondemand` governor goes to full speed at 25% load, and the
  pipeline keeps one core about half busy, so the CPU sat at 1464 MHz and 1.40 V for 96%
  of the time (this chip has no deep idle state). `rsp-cpufreq.service` sets the threshold
  to 85%: it then ran 75% of the time at 1200 MHz or below (1.275 V or less) with ODAS at
  60% of a core, and still rises under load. The work done is the same. The saving in
  watts has not been measured; the board has no power sensor.
- Mic gain is 14 dB at the converters, and `rsp-aec --gain 10` (in `STT_AEC_ARGS`) raises
  the microphones by 10 dB after cancellation, so a talker has the level they had at 24 dB.
  At 24 dB the board's own speaker clipped the converters (tens of thousands of samples
  piled up just under 0.9 of full scale on the two microphones nearest the speaker), and a
  clipped echo cannot be cancelled. At 14 dB the peaks were 0.34 to 0.63 of full scale and
  the canceller removed 16 to 17 dB of the speaker, against 8 to 11 dB on the clipped takes
  (different texts, 21 s against 65 s). The noise floor fell with the gain, so quiet speech
  should lose nothing; that was measured in a room with people talking, not in silence.
  With `STT_AEC=0` or without `rsp-aec` there is no make-up gain and the level thresholds
  are 10 dB too high.

## Licences

MIT (`LICENSE`) for everything except the kernel-derived files:

- `newos/port/` (AC108 driver, its kernel patch, Kconfig/Makefile changes and reference
  sources) and `newos/vendor-4.4/` (Seeed's kernel patches and config): GPL-2.0, text in
  `LICENSES/GPL-2.0-only.txt`.
- Device trees and overlays (`*.dts`, `*.dtsi`, `*.dtso`): GPL-2.0-or-later or MIT at your
  option, as in the kernel sources they derive from.

Files that carry their own licence notice or SPDX tag are under that licence. The ODAS patches
in `newos/cross/patches/` are MIT, as ODAS is.
The `rsp-aec` program built by `newos/cross/build-aec.sh` links webrtc-audio-processing
(BSD-3-Clause), abseil (Apache-2.0) and libspeexdsp (BSD-3-Clause); none of them is in this
repository.
