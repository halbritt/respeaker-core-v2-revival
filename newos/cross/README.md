# ODAS `armhf` Cross-Build (`newos/cross/`)

Cross-compiles [introlab/odas](https://github.com/introlab/odas) (pinned base commit `004cb82`) for Debian 13 (`trixie`) `armhf` on ReSpeaker Core v2 (`rk3229`).

## Layout

- `Dockerfile`: `debian:trixie` + `armhf` cross toolchain and `libfftw3-dev:armhf`, `libconfig-dev:armhf`, `libasound2-dev:armhf`, `libpulse-dev:armhf`.
- `cross.sh`: Wrapper to execute commands inside the `rsp-cross` Docker container with `newos/cross` mounted at `/work`.
- `build-odas.sh`: Idempotently applies `patches/*.patch` (auto-detecting `-p0` vs `-p1`) to `src/odas` and builds both `out/odas-stock/` and `out/odas-a7/` (`-mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard`).
- `patches/`: Patch series applied on top of `introlab/odas` `004cb82`:
  - `0001-steer2demixing-ds-zero-masked-channel-column.patch`: Zero full frequency column for masked channels (`RESPEAKER-4`).
  - `0002-src-hops-recover-from-alsa-xruns.patch`: ALSA xrun/suspend/EINTR recovery via `snd_pcm_recover()` + short-read accumulation (`RESPEAKER-5`).
  - `0003`-`0008`: Fixes for 6 latent ODAS bugs (`transcendental.c`, `track2gain.c`, `mod_ssl.c`, `env2env.c`, `fft.c`, `snk_tracks.c`; `RESPEAKER-6`).
  Only `0001` runs on the board so far; `0002` is unused there (ODAS reads from a FIFO); `0003`-`0008` were replayed on recorded takes. See the table in the top-level README.
- `upstream/`: Archival upstream PR notes (`RESPEAKER-4-steer2demixing-pr.md`).

## Usage

```sh
docker build -t rsp-cross newos/cross
git clone https://github.com/introlab/odas.git newos/cross/src/odas
git -C newos/cross/src/odas checkout 004cb82
./newos/cross/cross.sh ./build-odas.sh
```

Do not commit `src/` or `out/` to git.

## Echo canceller

`build-aec.sh` builds `out/rsp-aec/rsp-aec` from `board/rsp-aec.cc` (mounted at `/board` by
`cross.sh`). It downloads webrtc-audio-processing 1.3 (checked by SHA-256) into `src/`, builds
it as a static library with NEON, and links it in together with the library's own pinned
abseil, so the board needs only `libspeexdsp1` at run time.

```sh
./newos/cross/cross.sh ./build-aec.sh
```

`mk-sd-image.sh` installs the result as `/opt/rsp-stt/rsp-aec`.
