# `RESPEAKER-4`: Archival Upstream PR Packet (`steer2demixing_ds_process`)

> **Review note (2026-11-17).** The sanitizer harness described below is not in this
> repository, and the statement that it was built with `-Wall -Wextra -Werror` cannot be
> right: `steer2demixing.c` does not compile under `-Werror` with or without the patch. An
> independent harness written during review confirmed the behaviour claims (crash when all
> channels are masked, wrong column zeroed on a partial mask, identical output when nothing
> is masked). The fix was submitted upstream by someone else as introlab/odas PR #309; this
> project only added a confirming comment there.

## Upstream Status

- **Upstream PR**: [`introlab/odas#309`](https://github.com/introlab/odas/pull/309) (*"Fix uninitialised index when zeroing masked channels in steer2demixing_ds_process"*, opened 2026-10-30).
- **Owner Confirmation Comment**: [`introlab/odas#309#issuecomment-6941360553`](https://github.com/introlab/odas/pull/309#issuecomment-6941360553) (posted 2026-11-17T07:11:11Z).
- **Action Rule**: **Do NOT open a duplicate PR** while `introlab/odas#309` remains active. This packet is preserved solely for archival reference and fallback submission if PR #309 stalls or is closed unmerged.

---

## Problem Analysis

In `introlab/odas` commit `004cb82` (`src/system/steer2demixing.c`), `steer2demixing_ds_process()` has a bug when zeroing masked microphone channels:

1. `src/system/steer2demixing.c:65` declares `unsigned int iSampleBC;` without initialization.
2. In Loop 1 (`src/system/steer2demixing.c:86-108`), `iSampleBC` is only assigned inside `if (mask == 1)`.
3. In Loop 2 (`src/system/steer2demixing.c:110-140`):
   - When `mask == 1` (lines 115-131), the code loops `for (iBin = 0; iBin < obj->halfFrameSize; iBin++)`, sets `iSampleBC = iBin * obj->nChannels + iChannel`, and writes `demixings->array[iSep][iSampleBC * 2 + 0/1]`.
   - When `mask != 1` (`else` branch, lines 133-138), there is no `iBin` loop and `iSampleBC` is not assigned at all; it writes a single pair:
     ```c
     demixings->array[iSep][iSampleBC * 2 + 0] = 0.0f;
     demixings->array[iSep][iSampleBC * 2 + 1] = 0.0f;
     ```
4. **Consequences**:
   - **All channels masked** (e.g., 6-mic planar ring on ReSpeaker Core v2 with stock ODAS mic `angle = (80, 90)` for targets below ~4.5 deg elevation when `sss.mode_sep = "dds"`): `iSampleBC` is uninitialized stack garbage, causing an out-of-bounds write (`SIGSEGV` / heap corruption).
   - **Partial masking**: `iSampleBC` retains the stale value `(obj->halfFrameSize - 1) * obj->nChannels + prevUnmaskedChannel` from the preceding unmasked channel. The masked channel's bins are never zeroed, and the last frequency bin of the preceding unmasked channel is overwritten with `(0.0f, 0.0f)`.

---

## Patch Reference & Application Note

- **Local patch**: `newos/cross/patches/0001-steer2demixing-ds-zero-masked-channel-column.patch`
- **Target upstream base**: `introlab/odas` commit `004cb82` (`004cb824c6073771cb23cf9139f01e9d8581025b`)
- **Application format**: The patch file is a `-p0` unified diff (`+++ src/system/steer2demixing.c`). Apply with:
  ```sh
  git apply -p0 newos/cross/patches/0001-steer2demixing-ds-zero-masked-channel-column.patch
  # or: patch -p0 < newos/cross/patches/0001-steer2demixing-ds-zero-masked-channel-column.patch
  ```
  (`newos/cross/build-odas.sh` auto-detects `-p0` vs `-p1` and applies the full patch series idempotently.)

---

## Reproducer & Verification Summary

Verified against `introlab/odas` `004cb82` using a standalone C harness (`steer2demixing.c`, `demixing.c`, `mask.c`, `steer.c`, `track.c`, `cmatrix.c`) compiled with `gcc -O2 -g -Wall -Wextra -Werror -fsanitize=address,undefined` (`nSeps=1`, `nChannels=6`, `halfFrameSize=129`, `demixings` pre-filled with `(999.0f, -999.0f)`):

- **Unpatched `004cb82`**:
  - Compiler emits `error: 'iSampleBC' may be used uninitialized [-Werror=maybe-uninitialized]` at `src/system/steer2demixing.c:135`.
  - Under partial masking (`{0, 1, 1, 0, 1, 0}`), masked channel bins remain `(999.0f, -999.0f)` and unmasked channel 4 bin 128 is corrupted to `(0.0f, 0.0f)`.
- **Patched (`0001-steer2demixing-ds-zero-masked-channel-column.patch`)**:
  - `-Wmaybe-uninitialized` warning eliminated.
  - **All-masked (`{0, 0, 0, 0, 0, 0}`)**: All `129 * 6 * 2` entries cleanly zeroed with zero ASan/UBSan findings (`R4_ALL_OK`).
  - **Partial-masked (`{0, 1, 1, 0, 1, 0}`)**: All bins of masked channels `{0, 3, 5}` are `(0.0f, 0.0f)`; all bins of unmasked channels `{1, 2, 4}` are preserved uncorrupted.
  - **All-unmasked (`{1, 1, 1, 1, 1, 1}`)**: Binary output is byte-identical (`cmp -s` exit 0, `R4_BYTE_IDENTICAL_OK`) to unpatched `004cb82`.

---

## Suggested Commit Message, PR Title & Body (Fallback Only)

### Suggested Commit Message & PR Title
```
steer2demixing: zero full column for masked channels
```

### Suggested PR Body
```markdown
### Summary
Fix an uninitialized/stale index write in `steer2demixing_ds_process()` when a microphone channel is masked (`mask != 1`).

### Problem
In `src/system/steer2demixing.c`, `iSampleBC` is declared uninitialized (`line 65`) and only assigned inside the `if (mask == 1)` loops. In the `else` branch (`lines 133-138`), `demixings->array[iSep][iSampleBC * 2 + 0/1] = 0.0f` writes a single complex pair through `iSampleBC` without iterating over `iBin`:
- If all channels are masked for a source, `iSampleBC` is uninitialized, causing an out-of-bounds write / crash.
- If some channels are masked, `iSampleBC` retains the stale index of the previous unmasked channel's last frequency bin, corrupting that unmasked bin while leaving the masked channel's column unzeroed.

### Fix
Loop `iBin` from `0` to `obj->halfFrameSize - 1` inside the `else` branch and recompute `iSampleBC = iBin * obj->nChannels + iChannel` before zeroing each bin, matching the `if (mask == 1)` branch.

### Verification
Tested with AddressSanitizer and UndefinedBehaviorSanitizer (`nSeps=1, nChannels=6, halfFrameSize=129`):
- All-masked input zeros the entire `demixings` matrix without out-of-bounds writes.
- Partial-masked input zeros masked columns without corrupting unmasked channels.
- All-unmasked input produces byte-identical output to unpatched `004cb82`.
```
