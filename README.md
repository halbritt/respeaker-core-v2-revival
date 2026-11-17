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
| `board/` | Runtime: `rsp-stt.py`, its settings and unit, the ODAS config, `rsp-record`, `rsp-replay`, `rsp-listen`, `rsp-leds` |
| `newos/port/` | Board device tree, AC108 driver port and kernel patch, reference sources |
| `newos/mk-sd-image.sh` | Assembles the SD image from an Armbian rootfs plus our kernel, dtb and services |
| `newos/overlay/`, `newos/debug-initrd/` | Boot safety net and bring-up tools |
| `newos/cross/` | Docker cross toolchain (Debian 13 armhf) and the ODAS build with patches |
| `newos/vendor-4.4/` | Vendor kernel config and Seeed's kernel patches, for reference |
| `tools/bin/rsp-serial` | Run a command on the vendor image's USB serial console |
| `tests/` | Host test suite: `mise run test`, or `python3 -m pytest tests` (needs pytest and numpy; some tests skip without `dtc`, a static busybox or the upstream sources) |

Not in the repository (downloaded or generated): the Armbian build tree, Seeed's image, the
Rockchip loader tools, kernel/rootfs output, a static busybox for the initramfs.

## Audio pipeline

```
arecord (8 ch, 16 kHz, S32) --FIFO--> odaslive --+-- tracked sources (JSON, TCP 9000)
        |                                        +-- postfiltered audio (TCP 9002)
        +--> ~/recordings/*.raw (on request)     +-- separated audio    (TCP 9003)
                                                        |
                              rsp-stt: follow the loudest source, VAD, utterances
                                                        |
                                    whisper server  -->  transcripts.jsonl, /events
```

- `rsp-stt` owns the sound card and feeds ODAS through a FIFO, so it can save the raw capture
  while transcription carries on, and a capture overrun does not end the pipeline.
- The postfiltered stream (noise-masked) decides when someone is speaking; the separated
  stream (plain beamformer) is what is sent to Whisper, which recognises it slightly better.
- `STT_ODAS_MODE=dominant` (default) produces one stream that follows the loudest tracked
  source, so a talker who moves, or whose reflection is tracked as a second source, stays one
  utterance. `separate` segments each tracked source on its own.
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
(azimuth), `el` (elevation), `src` (ODAS track id), `t` (seconds since start).

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
stream of `speech_start`, `speech_end` and `utterance`; `GET /recent` returns the last
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
| Someone is speaking | Blue pointer towards the talker (whole ring dim if the direction is unknown) |
| Utterance being transcribed | Amber pointer, for at most 8 s |
| Transcript arrived | Green pointer for 0.6 s |
| Otherwise | Dark, also when the speech service is down |

The ring's supply is switched by GPIO2_A2 (low = on), held by the `rsp-led-power` service.
LED 0 is at Mic 2 (azimuth 330) and the numbers run clockwise seen from above.

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
difference. `odaslive` uses 60-80% of one core.

## OS image

`newos/mk-sd-image.sh` lays an Armbian trixie rootfs and our kernel out the way Seeed's
bootloader expects (`/boot/uEnv.txt`, kernel, dtb and initrd on ext4 partition 2). With the
card inserted the board boots it; without it, the system on the eMMC boots.

```
# in newos/armbian-build (Armbian framework, with port/ files under userpatches/):
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

- The image built by `mk-sd-image.sh` has not yet been flashed and booted from scratch; the
  running board was brought up step by step.
- The new OS runs from the SD card. The eMMC works under 6.18 (clock limited to 37.5 MHz),
  but installing to it is not scripted.
- Playback (the on-chip DAC), echo cancellation and a local wake-word model are not set up.
  The loopback channels are captured but unused.
- The kernel's USB PHY use-after-free (see above) is worked around in the device tree; a fix
  for the driver is in `newos/port/patches/` and has not been sent upstream.
- Mic gain is 24 dB: at 28 dB a talker close to the board peaked at -0.8 dBFS. Not calibrated further.

## Licences

MIT (`LICENSE`) for everything except the kernel-derived files:

- `newos/port/` (AC108 driver, its kernel patch, Kconfig/Makefile changes and reference
  sources) and `newos/vendor-4.4/` (Seeed's kernel patches and config): GPL-2.0, text in
  `LICENSES/GPL-2.0-only.txt`.
- Device trees and overlays (`*.dts`, `*.dtsi`, `*.dtso`): GPL-2.0-or-later or MIT at your
  option, as in the kernel sources they derive from.

Files that carry their own licence notice or SPDX tag are under that licence. The ODAS patches
in `newos/cross/patches/` are MIT, as ODAS is.
