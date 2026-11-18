#!/bin/sh
# Cross-build rsp-aec, the echo canceller (run inside the container: ./cross.sh ./build-aec.sh).
# Fetches webrtc-audio-processing (the PulseAudio packaging of WebRTC's audio processing, with
# AEC3), builds it as a static library with NEON, and links board/rsp-aec.cc against it.
# Debian's package of the same library is built without NEON and does not install the AEC3
# headers the tool needs. The Speex echo canceller is built from source too, in floating
# point with -O3 -ffast-math: measured on the board that is 26% cheaper than Debian's
# library at the same cancellation (the compiler vectorises the filter loops with NEON;
# hand-written NEON for them gained 2% more, FFTW in place of Speex's own FFT nothing, and
# the fixed-point build nothing). Produces out/rsp-aec/rsp-aec, which needs no extra library
# at run time.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"

V=1.3
SUM=2365e93e778d7b61b5d6e02d21c47d97222e9c7deff9e1d0838ad6ec2e86f1b9
URL=https://freedesktop.org/software/pulseaudio/webrtc-audio-processing/webrtc-audio-processing-$V.tar.xz
SRC=src/webrtc-audio-processing-$V
[ -f /board/rsp-aec.cc ] || { echo "ERROR: /board/rsp-aec.cc not found (run through ./cross.sh)" >&2; exit 1; }

if [ ! -d "$SRC" ]; then
  mkdir -p src
  curl -fsSL -o /tmp/wap.tar.xz "$URL"
  echo "$SUM  /tmp/wap.tar.xz" | sha256sum -c - >/dev/null
  tar -xJf /tmp/wap.tar.xz -C src
fi

SV=1.2.1
SSUM=8c777343e4a6399569c72abc38a95b24db56882c83dbdb6c6424a5f4aeb54d3d
SURL=https://downloads.xiph.org/releases/speex/speexdsp-$SV.tar.gz
SSRC=src/speexdsp-$SV
if [ ! -d "$SSRC" ]; then
  mkdir -p src
  curl -fsSL -o /tmp/speexdsp.tar.gz "$SURL"
  echo "$SSUM  /tmp/speexdsp.tar.gz" | sha256sum -c - >/dev/null
  tar -xzf /tmp/speexdsp.tar.gz -C src
fi
# the echo canceller only: its filter, the FFT wrapper and Speex's own FFT
sx=/tmp/build-speex; rm -rf "$sx"; mkdir -p "$sx/speex"
cp "$SSRC"/include/speex/*.h "$sx/speex/"
printf '#include <stdint.h>\ntypedef int16_t spx_int16_t;\ntypedef uint16_t spx_uint16_t;\ntypedef int32_t spx_int32_t;\ntypedef uint32_t spx_uint32_t;\n' \
  > "$sx/speex/speexdsp_config_types.h"
for f in mdf fftwrap smallft; do
  arm-linux-gnueabihf-gcc $RSP_CPU_FLAGS -O3 -ffast-math -DFLOATING_POINT -DUSE_SMALLFT -DEXPORT= -DVAR_ARRAYS \
    -I"$sx" -I"$SSRC/libspeexdsp" -c "$SSRC/libspeexdsp/$f.c" -o "$sx/$f.o"
done

cat > /tmp/armhf.ini <<INI
[binaries]
c = 'arm-linux-gnueabihf-gcc'
cpp = 'arm-linux-gnueabihf-g++'
ar = 'arm-linux-gnueabihf-ar'
strip = 'arm-linux-gnueabihf-strip'
pkg-config = 'pkg-config'

[built-in options]
c_args = [$(printf "'%s', " ${RSP_CPU_FLAGS:?RSP_CPU_FLAGS must be set})]
cpp_args = [$(printf "'%s', " $RSP_CPU_FLAGS)]

[host_machine]
system = 'linux'
cpu_family = 'arm'
cpu = 'armv7'
endian = 'little'
INI

b=/tmp/build-wap; rm -rf "$b"
# abseil comes from the library's own pinned subproject and is linked in, so the board needs no extra package
meson setup "$b" "$SRC" --cross-file /tmp/armhf.ini --wrap-mode=forcefallback \
  -Dneon=yes -Ddefault_library=static -Dbuildtype=release >/dev/null
ninja -C "$b" >/dev/null
lib=$b/webrtc/modules/audio_processing/libwebrtc-audio-processing-1.a
[ -f "$lib" ] || lib=$(find "$b" -name 'libwebrtc-audio-processing-1.a' | head -1)

rm -rf out/rsp-aec; mkdir -p out/rsp-aec
arm-linux-gnueabihf-g++ -O2 $RSP_CPU_FLAGS -DWEBRTC_POSIX -DWEBRTC_LIBRARY_IMPL -DWEBRTC_HAS_NEON \
  -DWEBRTC_APM_DEBUG_DUMP=0 -I"$SRC/webrtc" -I"$SRC/subprojects/abseil-cpp-20230125.1" \
  -o out/rsp-aec/rsp-aec /board/rsp-aec.cc "$lib" $(find "$b/subprojects" -name 'libabsl_*.a' | sort) \
  "$sx"/mdf.o "$sx"/fftwrap.o "$sx"/smallft.o -lm -lrt -pthread
arm-linux-gnueabihf-strip out/rsp-aec/rsp-aec
file out/rsp-aec/rsp-aec | cut -c1-120
arm-linux-gnueabihf-readelf -d out/rsp-aec/rsp-aec | grep NEEDED | tr -s ' ' | tr '\n' ' '; echo
arm-linux-gnueabihf-readelf -A out/rsp-aec/rsp-aec | grep -E 'Tag_FP_arch|Tag_Advanced_SIMD' | tr -s ' ' | tr '\n' ' '; echo
