"""F4: ODAS Patch Stack (`newos/cross/`) verification tests."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from conftest import NEWOS_DIR

CROSS_DIR = NEWOS_DIR / "cross"
PATCHES_DIR = CROSS_DIR / "patches"

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(patch_text: str) -> list[dict]:
    """Parse a unified diff into per-file hunk lists preserving exact whitespace."""
    files: list[dict] = []
    lines = patch_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old_path = line[4:].split("\t", 1)[0].strip()
            new_path = lines[i + 1][4:].split("\t", 1)[0].strip()
            hunks: list[dict] = []
            i += 2
            while i < len(lines):
                if lines[i].startswith(("diff --git ", "--- ")):
                    break
                m = HUNK_RE.match(lines[i])
                if m:
                    old_start = int(m.group(1))
                    old_count = int(m.group(2)) if m.group(2) is not None else 1
                    new_start = int(m.group(3))
                    new_count = int(m.group(4)) if m.group(4) is not None else 1
                    i += 1
                    body: list[str] = []
                    while i < len(lines):
                        cur = lines[i]
                        if cur.startswith(("@@ ", "diff --git ", "--- ", "-- \n", "--\n")):
                            break
                        if cur.startswith((" ", "-", "+", "\\")):
                            body.append(cur)
                            i += 1
                        else:
                            break
                    hunks.append(
                        {
                            "old_start": old_start,
                            "old_count": old_count,
                            "new_start": new_start,
                            "new_count": new_count,
                            "body": body,
                        }
                    )
                else:
                    i += 1
            files.append({"old_path": old_path, "new_path": new_path, "hunks": hunks})
            continue
        i += 1
    return files


def _pflag_for_patch(patch_text: str) -> str:
    if re.search(r"^(--- a/|\+\+\+ b/|diff --git a/)", patch_text, re.MULTILINE):
        return "-p1"
    return "-p0"


def _rel_target_path(file_entry: dict, pflag: str) -> Path:
    new_p = file_entry["new_path"]
    if pflag == "-p1" and new_p.startswith("b/"):
        return Path(new_p[2:])
    return Path(new_p)


def build_preimage_tree(root: Path, patch_text: str) -> str:
    """Reconstruct synthetic pre-image files for all hunks in `patch_text` under `root`."""
    pflag = _pflag_for_patch(patch_text)
    for fentry in parse_unified_diff(patch_text):
        rel = _rel_target_path(fentry, pflag)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        buf: list[str] = []
        for h in sorted(fentry["hunks"], key=lambda x: x["old_start"]):
            while len(buf) < max(0, h["old_start"] - 1):
                buf.append(f"/* pad {len(buf) + 1} */\n")
            for bline in h["body"]:
                if bline.startswith((" ", "-")):
                    buf.append(bline[1:])
        buf.extend(["/* pad tail 1 */\n", "/* pad tail 2 */\n"])
        target.write_text("".join(buf), encoding="utf-8")
    return pflag


def _strip_c_comments_and_strings(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_sl = in_ml = False
    quote: str | None = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_sl:
            if ch == "\n":
                in_sl = False
                out.append(ch)
            i += 1
        elif in_ml:
            if ch == "*" and nxt == "/":
                in_ml = False
                i += 2
            else:
                i += 1
        elif quote is not None:
            if ch == "\\" and i + 1 < n:
                i += 2
            elif ch == quote:
                quote = None
                i += 1
            else:
                i += 1
        else:
            if ch == "/" and nxt == "/":
                in_sl = True
                i += 2
            elif ch == "/" and nxt == "*":
                in_ml = True
                i += 2
            elif ch in ('"', "'"):
                quote = ch
                i += 1
            else:
                out.append(ch)
                i += 1
    return "".join(out)


def _setup_mock_build_odas(tmp_path: Path, cmake_rc: int = 0) -> tuple[Path, Path, Path, dict[str, str]]:
    work = tmp_path / "cross_work"
    src_odas = work / "src" / "odas"
    patches_dst = work / "patches"
    bin_dir = tmp_path / "mock_bin"
    log_file = tmp_path / "cmake_calls.log"
    work.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    shutil.copytree(PATCHES_DIR, patches_dst)

    # Reconstruct pre-image stubs for all patches so build-odas.sh patch loop exercises real files
    for p in sorted(patches_dst.glob("*.patch")):
        build_preimage_tree(src_odas, p.read_text(encoding="utf-8"))

    script = (CROSS_DIR / "build-odas.sh").read_text(encoding="utf-8")
    script = script.replace("/tmp/build-$v", f"{tmp_path}/build-$v")
    build_sh = work / "build-odas.sh"
    build_sh.write_text(script, encoding="utf-8")
    build_sh.chmod(0o755)

    (bin_dir / "cmake").write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> {str(log_file)!r}\n"
        f"[ {cmake_rc} -eq 0 ] || exit {cmake_rc}\n"
        "b=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-B\" ]; then b=\"$2\"; shift 2; continue; fi\n"
        "  shift\n"
        "done\n"
        "mkdir -p \"$b/bin\" \"$b/lib\"\n"
        "printf 'FAKEELF' > \"$b/bin/odaslive\"\n"
        "printf 'FAKESO' > \"$b/lib/libodas.so\"\n",
        encoding="utf-8",
    )
    (bin_dir / "ninja").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "file").write_text("#!/bin/sh\necho \"$1: ELF 32-bit LSB executable, ARM\"\n", encoding="utf-8")
    (bin_dir / "arm-linux-gnueabihf-readelf").write_text(
        "#!/bin/sh\necho 'RUNPATH $ORIGIN/../lib'\n", encoding="utf-8"
    )
    for name in ("cmake", "ninja", "file", "arm-linux-gnueabihf-readelf"):
        (bin_dir / name).chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "RSP_CPU_FLAGS": "-mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard",
    }
    return work, build_sh, log_file, env


# --- Tier 1: Happy-Path ODAS Patch Stack (6 tests) ---


def test_f4_t1_odas_patches_exist_and_sequential() -> None:
    patches = sorted(PATCHES_DIR.glob("*.patch"))
    assert patches, "Expected at least one patch in newos/cross/patches/"
    prefixes: list[int] = []
    for p in patches:
        m = re.match(r"^(\d{4})-[A-Za-z0-9_.-]+\.patch$", p.name)
        assert m is not None, f"Unexpected patch filename format: {p.name}"
        prefixes.append(int(m.group(1)))
    assert len(prefixes) == len(set(prefixes)), f"Duplicate patch prefixes: {prefixes}"
    assert prefixes == sorted(prefixes) and prefixes[0] == 1


def test_f4_t1_odas_patches_valid_unified_diff_hunks() -> None:
    patches = sorted(PATCHES_DIR.glob("*.patch"))
    assert patches
    for p in patches:
        files = parse_unified_diff(p.read_text(encoding="utf-8"))
        assert files, f"No file diffs parsed in {p.name}"
        for fentry in files:
            assert fentry["hunks"], f"No hunks in {p.name} for {fentry['new_path']}"
            for h in fentry["hunks"]:
                old_lines = sum(1 for line in h["body"] if line.startswith((" ", "-")))
                new_lines = sum(1 for line in h["body"] if line.startswith((" ", "+")))
                assert old_lines == h["old_count"], f"{p.name}: expected {h['old_count']} old lines, got {old_lines}"
                assert new_lines == h["new_count"], f"{p.name}: expected {h['new_count']} new lines, got {new_lines}"


def test_f4_t1_odas_patches_apply_to_synthetic_tree(tmp_path: Path) -> None:
    patches = sorted(PATCHES_DIR.glob("*.patch"))
    assert patches
    for p in patches:
        tree = tmp_path / p.stem
        text = p.read_text(encoding="utf-8")
        pflag = build_preimage_tree(tree, text)
        patch_check = subprocess.run(
            ["patch", "-d", str(tree), pflag, "--dry-run", "-i", str(p)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert patch_check.returncode == 0, f"patch --dry-run failed for {p.name}: {patch_check.stdout} {patch_check.stderr}"
        git_check = subprocess.run(
            ["git", "apply", pflag, "--check", str(p)],
            cwd=tree,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert git_check.returncode == 0, f"git apply --check failed for {p.name}: {git_check.stderr}"

    p0001 = (PATCHES_DIR / "0001-steer2demixing-ds-zero-masked-channel-column.patch").read_text(encoding="utf-8")
    assert "for (iBin = 0; iBin < obj->halfFrameSize; iBin++)" in p0001


def test_f4_t1_build_odas_sh_variants_and_rpath_flags() -> None:
    build_sh = CROSS_DIR / "build-odas.sh"
    res = subprocess.run(["sh", "-n", str(build_sh)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    txt = build_sh.read_text(encoding="utf-8")
    assert "for v in stock a7" in txt
    assert "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON" in txt
    assert "out/odas-$v/bin" in txt and "out/odas-$v/lib" in txt


def test_f4_t1_dockerfile_and_cross_sh_toolchain_contract() -> None:
    df = (CROSS_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM debian:trixie" in df
    assert "dpkg --add-architecture armhf" in df
    for pkg in ("libfftw3-dev:armhf", "libconfig-dev:armhf", "libasound2-dev:armhf", "libpulse-dev:armhf"):
        assert pkg in df
    assert 'RSP_CPU_FLAGS="-mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard"' in df

    cross_sh = CROSS_DIR / "cross.sh"
    res = subprocess.run(["sh", "-n", str(cross_sh)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert '"$here":/work' in cross_sh.read_text(encoding="utf-8")


def test_f4_t1_build_odas_mocked_execution_and_patch_integration(tmp_path: Path) -> None:
    work, build_sh, _, env = _setup_mock_build_odas(tmp_path, cmake_rc=0)
    res1 = subprocess.run(["sh", str(build_sh)], cwd=work, env=env, capture_output=True, text=True, check=False)
    assert res1.returncode == 0, f"{res1.stdout}\n{res1.stderr}"
    for variant in ("odas-stock", "odas-a7"):
        assert (work / "out" / variant / "bin" / "odaslive").is_file()
        assert (work / "out" / variant / "lib" / "libodas.so").is_file()

    # M1 RESPEAKER-5: verify build-odas.sh applies patches/*.patch and two consecutive runs are idempotent
    assert "patches/*.patch" in build_sh.read_text(encoding="utf-8")
    res2 = subprocess.run(["sh", str(build_sh)], cwd=work, env=env, capture_output=True, text=True, check=False)
    assert res2.returncode == 0, f"Second run not idempotent: {res2.stdout}\n{res2.stderr}"
    assert "Patch already applied:" in res2.stdout


# --- Tier 2: Boundary, Corner & Error Paths (6 tests) ---


def test_f4_t2_odas_patches_no_conflict_markers_or_rejects() -> None:
    for p in PATCHES_DIR.iterdir():
        assert p.suffix == ".patch", f"Unexpected non-patch artifact in patches/: {p.name}"
        txt = p.read_text(encoding="utf-8")
        for marker in ("<<<<<<<", ">>>>>>>"):
            assert marker not in txt, f"Conflict marker {marker} in {p.name}"


def test_f4_t2_odas_patch_fails_on_mismatched_preimage(tmp_path: Path) -> None:
    p0001 = PATCHES_DIR / "0001-steer2demixing-ds-zero-masked-channel-column.patch"
    tree = tmp_path / "bad_tree"
    pflag = build_preimage_tree(tree, p0001.read_text(encoding="utf-8"))
    target = tree / "src" / "system" / "steer2demixing.c"
    target.write_text("/* corrupted incompatible preimage */\n", encoding="utf-8")

    patch_res = subprocess.run(
        ["patch", "-d", str(tree), pflag, "--dry-run", "-i", str(p0001)],
        capture_output=True,
        text=True,
        check=False,
    )
    git_res = subprocess.run(
        ["git", "apply", pflag, "--check", str(p0001)],
        cwd=tree,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert patch_res.returncode != 0
    assert git_res.returncode != 0


def test_f4_t2_odas_patch_added_c_blocks_brace_balanced() -> None:
    for p in sorted(PATCHES_DIR.glob("*.patch")):
        for fentry in parse_unified_diff(p.read_text(encoding="utf-8")):
            removed_src = "".join(
                line[1:] for h in fentry["hunks"] for line in h["body"] if line.startswith("-")
            )
            added_src = "".join(
                line[1:] for h in fentry["hunks"] for line in h["body"] if line.startswith("+")
            )
            rem_clean = _strip_c_comments_and_strings(removed_src)
            add_clean = _strip_c_comments_and_strings(added_src)
            for opener, closer in (("{", "}"), ("(", ")")):
                rem_net = rem_clean.count(opener) - rem_clean.count(closer)
                add_net = add_clean.count(opener) - add_clean.count(closer)
                assert add_net == rem_net, (
                    f"{p.name} ({fentry['new_path']}): unbalanced {opener}{closer} "
                    f"(removed net={rem_net}, added net={add_net})"
                )


def test_f4_t2_odas_patch_target_relpaths_safe_and_m1_targets_covered() -> None:
    patches = sorted(PATCHES_DIR.glob("*.patch"))
    target_paths: set[str] = set()
    for p in patches:
        text = p.read_text(encoding="utf-8")
        pflag = _pflag_for_patch(text)
        for fentry in parse_unified_diff(text):
            rel = _rel_target_path(fentry, pflag)
            assert not rel.is_absolute(), f"{p.name}: absolute +++ path {rel}"
            assert ".." not in rel.parts, f"{p.name}: traversal in +++ path {rel}"
            assert rel.parts[0] == "src", f"{p.name}: expected src/ target, got {rel}"
            target_paths.add(rel.as_posix())

    build_sh_txt = (CROSS_DIR / "build-odas.sh").read_text(encoding="utf-8")
    assert len(patches) > 1 and re.search(
        r"^\s*for\s+\S+\s+in\s+patches/\*\.patch(?=[;\s]|$)", build_sh_txt, re.MULTILINE
    ), "M1 RESPEAKER-5/6 patches or build-odas.sh patch loop missing"

    expected_m1_targets = {
        "src/system/steer2demixing.c",
        "src/source/src_hops.c",
        "src/utils/transcendental.c",
        "src/system/track2gain.c",
        "src/module/mod_ssl.c",
        "src/system/env2env.c",
        "src/utils/fft.c",
        "src/sink/snk_tracks.c",
    }
    assert expected_m1_targets.issubset(target_paths)
    src_hops_patch = [p for p in patches if "src_hops" in p.name or "snd_pcm_recover" in p.read_text(encoding="utf-8")]
    assert src_hops_patch and "snd_pcm_recover" in src_hops_patch[0].read_text(encoding="utf-8")


def test_f4_t2_build_odas_aborts_on_cmake_or_patch_failure(tmp_path: Path) -> None:
    work, build_sh, _, env = _setup_mock_build_odas(tmp_path, cmake_rc=2)
    res = subprocess.run(["sh", str(build_sh)], cwd=work, env=env, capture_output=True, text=True, check=False)
    assert res.returncode != 0
    assert not (work / "out" / "odas-stock").exists()
    assert not (work / "out" / "odas-a7").exists()


def test_f4_t2_odas_a7_cflags_isolated_from_stock(tmp_path: Path) -> None:
    work, build_sh, log_file, env = _setup_mock_build_odas(tmp_path, cmake_rc=0)
    res = subprocess.run(["sh", str(build_sh)], cwd=work, env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    calls = log_file.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert "-DCMAKE_C_FLAGS= " in calls[0] or calls[0].endswith("-DCMAKE_C_FLAGS=")
    assert f"-DCMAKE_C_FLAGS={env['RSP_CPU_FLAGS']}" in calls[1]
