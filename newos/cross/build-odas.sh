#!/bin/sh
# Cross-build ODAS for the board (run inside the container: ./cross.sh ./build-odas.sh).
# src/odas is expected at commit 004cb82; patches/*.patch are applied idempotently.
# Produces out/odas-stock/ (project flags only) and out/odas-a7/ (Cortex-A7 + NEON tuned).
# Layout: bin/odaslive + lib/libodas.so (rpath $ORIGIN/../lib); copy the directory whole.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$here"

[ -d src/odas ] || { echo "ERROR: src/odas not found (expected ODAS checkout at 004cb82)" >&2; exit 1; }

for p in patches/*.patch; do
  [ -f "$p" ] || continue
  abs_p="$here/$p"
  if grep -qE '^(--- a/|\+\+\+ b/|diff --git a/)' "$abs_p"; then
    pflag="-p1"
  else
    pflag="-p0"
  fi
  if (cd src/odas && GIT_CEILING_DIRECTORIES="$here/src" git apply "$pflag" -R --check "$abs_p" >/dev/null 2>&1); then
    echo "Patch already applied: $p"
  elif (cd src/odas && GIT_CEILING_DIRECTORIES="$here/src" git apply "$pflag" --check "$abs_p" >/dev/null 2>&1); then
    echo "Applying patch: $p"
    (cd src/odas && GIT_CEILING_DIRECTORIES="$here/src" git apply "$pflag" "$abs_p")
  else
    echo "ERROR: patch failed check (conflict or wrong base): $p" >&2
    (cd src/odas && GIT_CEILING_DIRECTORIES="$here/src" git apply "$pflag" --check "$abs_p") || true
    exit 1
  fi
done

for v in stock a7; do
  case $v in
    stock) extra="" ;;
    a7) extra="${RSP_CPU_FLAGS:?RSP_CPU_FLAGS must be set for a7 build}" ;;
  esac
  b="/tmp/build-$v"; rm -rf "$b"
  cmake -S src/odas -B "$b" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS="$extra" -DCMAKE_BUILD_RPATH_USE_ORIGIN=ON >/dev/null
  ninja -C "$b" >/dev/null
  rm -rf "out/odas-$v"; mkdir -p "out/odas-$v/bin" "out/odas-$v/lib"
  cp "$b/bin/odaslive" "out/odas-$v/bin/"; cp "$b/lib/libodas.so" "out/odas-$v/lib/"
  file "out/odas-$v/bin/odaslive" | cut -c1-120
  arm-linux-gnueabihf-readelf -d "out/odas-$v/bin/odaslive" | grep -E 'NEEDED|RUNPATH|RPATH' | tr -s ' ' | tr '\n' ' '; echo
  arm-linux-gnueabihf-readelf -A "out/odas-$v/bin/odaslive" | grep -E 'Tag_CPU_name|Tag_FP_arch|Tag_Advanced_SIMD' | tr -s ' ' | tr '\n' ' '; echo
done
