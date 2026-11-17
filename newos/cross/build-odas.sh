#!/bin/sh
# Cross-build ODAS for the board (run inside the container: ./cross.sh ./build-odas.sh).
# src/odas is expected at commit 004cb82 with patches/*.patch applied.
# Produces out/odas-stock/ (project flags only) and out/odas-a7/ (Cortex-A7 + NEON tuned).
# Layout: bin/odaslive + lib/libodas.so (rpath $ORIGIN/../lib); copy the directory whole.
set -e
for v in stock a7; do
  case $v in stock) extra="";; a7) extra="$RSP_CPU_FLAGS";; esac
  b=/tmp/build-$v; rm -rf $b
  cmake -S src/odas -B $b -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS="$extra" -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON >/dev/null
  ninja -C $b >/dev/null
  rm -rf out/odas-$v; mkdir -p out/odas-$v/bin out/odas-$v/lib
  cp $b/bin/odaslive out/odas-$v/bin/; cp $b/lib/libodas.so out/odas-$v/lib/
  file out/odas-$v/bin/odaslive | cut -c1-120
  arm-linux-gnueabihf-readelf -d out/odas-$v/bin/odaslive | grep -E 'NEEDED|RUNPATH|RPATH' | tr -s ' ' | tr '\n' ' '; echo
  arm-linux-gnueabihf-readelf -A out/odas-$v/bin/odaslive | grep -E 'Tag_CPU_name|Tag_FP_arch|Tag_Advanced_SIMD' | tr -s ' ' | tr '\n' ' '; echo
done
