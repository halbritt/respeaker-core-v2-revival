# Cross toolchain for the ReSpeaker Core v2 userland: Debian 13 (trixie) armhf, matching the
# board's libraries. Build:  docker build -t rsp-cross .   Use: ./cross.sh <command...>
FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN dpkg --add-architecture armhf && apt-get update && apt-get install -y --no-install-recommends \
      build-essential crossbuild-essential-armhf cmake ninja-build pkg-config git ca-certificates file \
      libfftw3-dev:armhf libconfig-dev:armhf libasound2-dev:armhf libpulse-dev:armhf \
      meson curl xz-utils python3 libspeexdsp-dev:armhf \
    && rm -rf /var/lib/apt/lists/*
# CMake toolchain file: target armhf, find libraries only in the armhf multiarch dirs
RUN printf '%s\n' \
  'set(CMAKE_SYSTEM_NAME Linux)' 'set(CMAKE_SYSTEM_PROCESSOR arm)' \
  'set(CMAKE_C_COMPILER arm-linux-gnueabihf-gcc)' 'set(CMAKE_CXX_COMPILER arm-linux-gnueabihf-g++)' \
  'set(CMAKE_LIBRARY_ARCHITECTURE arm-linux-gnueabihf)' \
  'set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)' 'set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)' \
  'set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE BOTH)' > /opt/armhf.cmake
ENV PKG_CONFIG_LIBDIR=/usr/lib/arm-linux-gnueabihf/pkgconfig:/usr/share/pkgconfig \
    CC=arm-linux-gnueabihf-gcc CXX=arm-linux-gnueabihf-g++ \
    CMAKE_TOOLCHAIN_FILE=/opt/armhf.cmake \
    RSP_CPU_FLAGS="-mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard"
WORKDIR /work
