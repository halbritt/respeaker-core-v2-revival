"""F5: Kernel & AC108 Patches (`newos/port/`) verification tests."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import NEWOS_DIR
from test_odas_patches import build_preimage_tree, parse_unified_diff

PORT_DIR = NEWOS_DIR / "port"
TREE_A = PORT_DIR / "tree" / "a"
TREE_B = PORT_DIR / "tree" / "b"
AC108_PATCH = PORT_DIR / "0001-ASoC-codecs-add-AC108-mic-array-driver-for-ReSpeaker.patch"
DTS_MAIN = PORT_DIR / "rk3229-respeaker-v2.dts"
DTS_MIN = NEWOS_DIR / "debug-initrd" / "rk3229-respeaker-v2-min.dts"


def _strip_dts_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _extract_dts_block(stripped_dts: str, header_re: str) -> str:
    m = re.search(header_re + r"\s*\{", stripped_dts)
    assert m is not None, f"DTS block matching {header_re!r} not found"
    start = m.end() - 1
    depth = 0
    for idx in range(start, len(stripped_dts)):
        ch = stripped_dts[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped_dts[start : idx + 1]
    raise AssertionError(f"Unclosed DTS block for {header_re!r}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Tier 1: Happy-Path Kernel & AC108 Patches (6 tests) ---


def test_f5_t1_ac108_c_matches_tree_b_byte_for_byte() -> None:
    src_c = PORT_DIR / "ac108.c"
    tree_c = TREE_B / "sound" / "soc" / "codecs" / "ac108.c"
    assert src_c.is_file() and tree_c.is_file()
    assert _sha256(src_c) == _sha256(tree_c), "newos/port/ac108.c and tree/b/.../ac108.c differ"

    src_h = PORT_DIR / "ac108.h"
    tree_h = TREE_B / "sound" / "soc" / "codecs" / "ac108.h"
    if src_h.is_file():
        assert tree_h.is_file()
        assert _sha256(src_h) == _sha256(tree_h), "newos/port/ac108.h and tree/b/.../ac108.h differ"


def test_f5_t1_ac108_patch_applies_cleanly_to_tree_a(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(TREE_A, tree)
    git_res = subprocess.run(
        ["git", "apply", "--check", str(AC108_PATCH)],
        cwd=tree,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert git_res.returncode == 0, f"git apply --check failed: {git_res.stderr}"

    patch_res = subprocess.run(
        ["patch", "-d", str(tree), "-p1", "--dry-run", "-i", str(AC108_PATCH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert patch_res.returncode == 0, f"patch --dry-run failed: {patch_res.stdout} {patch_res.stderr}"


def test_f5_t1_ac108_patch_applied_tree_matches_tree_b_exactly(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(TREE_A, tree)
    apply_res = subprocess.run(
        ["git", "apply", str(AC108_PATCH)],
        cwd=tree,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply_res.returncode == 0, apply_res.stderr

    applied_rel = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file())
    expected_rel = sorted(p.relative_to(TREE_B).as_posix() for p in TREE_B.rglob("*") if p.is_file())
    assert applied_rel == expected_rel
    for rel in expected_rel:
        assert _sha256(tree / rel) == _sha256(TREE_B / rel), f"Applied file differs from tree/b: {rel}"


def test_f5_t1_ac108_kconfig_makefile_and_dt_compat_wiring() -> None:
    kconfig = (TREE_B / "sound" / "soc" / "codecs" / "Kconfig").read_text(encoding="utf-8")
    assert "config SND_SOC_AC108" in kconfig
    assert "depends on I2C" in kconfig

    makefile = (TREE_B / "sound" / "soc" / "codecs" / "Makefile").read_text(encoding="utf-8")
    assert "snd-soc-ac108-y := ac108.o" in makefile
    assert "obj-$(CONFIG_SND_SOC_AC108)\t+= snd-soc-ac108.o" in makefile or "obj-$(CONFIG_SND_SOC_AC108) += snd-soc-ac108.o" in makefile

    ac108_c = (PORT_DIR / "ac108.c").read_text(encoding="utf-8")
    dts_txt = DTS_MAIN.read_text(encoding="utf-8")
    for compat in ("x-powers,ac108-micarray-0", "x-powers,ac108-micarray-1"):
        assert compat in ac108_c
        assert compat in dts_txt
    assert "static void ac108_i2c_remove(struct i2c_client *i2c)" in ac108_c


def test_f5_t1_dts_hardware_safety_invariants() -> None:
    stripped = _strip_dts_comments(DTS_MAIN.read_text(encoding="utf-8"))
    assert 'compatible = "seeed,respeaker-core-v2", "rockchip,rk3229";' in stripped

    emmc = _extract_dts_block(stripped, r"&emmc")
    assert "max-frequency = <37500000>;" in emmc
    assert 'status = "okay";' in emmc

    ramoops = _extract_dts_block(stripped, r"ramoops@90000000")
    assert 'compatible = "ramoops";' in ramoops
    assert "reg = <0x90000000 0x100000>;" in ramoops

    sound = _extract_dts_block(stripped, r"sound")
    assert 'compatible = "simple-audio-card";' in sound
    assert 'simple-audio-card,name = "seeed-8mic-voicecard";' in sound
    assert "simple-audio-card,mclk-fs = <256>;" in sound
    assert "sound-dai = <&i2s1>;" in sound
    assert "sound-dai = <&ac108_0>;" in sound

    i2s1 = _extract_dts_block(stripped, r"&i2s1")
    assert "rockchip,capture-channels = <8>;" in i2s1

    wdt = _extract_dts_block(stripped, r"&wdt")
    assert 'status = "okay";' in wdt

    usb_otg = _extract_dts_block(stripped, r"&usb_otg")
    assert 'dr_mode = "peripheral";' in usb_otg
    assert 'status = "okay";' in usb_otg


def _has_owner_gate_dco_doc(patches_dir: Path) -> bool:
    for md in sorted(patches_dir.glob("*.md")):
        md_txt = md.read_text(encoding="utf-8")
        if (
            "Signed-off-by:" in md_txt
            and re.search(r"\bowner\b", md_txt, re.IGNORECASE)
            and ("OWNER GATE" in md_txt or re.search(r"\bDCO\b", md_txt))
        ):
            return True
    return False


def _validate_port_patches_dir(patches_dir: Path, work_dir: Path) -> None:
    patches = sorted(patches_dir.glob("*.patch"))
    assert patches, f"{patches_dir} exists but contains no *.patch files"

    has_owner_gate_doc = _has_owner_gate_dco_doc(patches_dir)
    for p in patches:
        txt = p.read_text(encoding="utf-8")
        lines = txt.splitlines()
        assert lines and lines[0].startswith("From "), f"{p.name} missing format-patch header 'From '"
        for label, pattern in (
            ("Date:", r"^Date:\s+\S+"),
            ("Subject: [PATCH", r"^Subject:\s+\[PATCH"),
            ("---", r"^---$"),
            ("diff --git a/... b/...", r"^diff --git a/\S+ b/\S+$"),
        ):
            assert re.search(pattern, txt, re.MULTILINE), f"{p.name} missing format-patch header {label!r}"

        has_sob = bool(re.search(r"^Signed-off-by:\s+\S.+<\S+@\S+>", txt, re.MULTILINE))
        assert has_sob or has_owner_gate_doc, (
            f"{p.name} missing 'Signed-off-by:' and {patches_dir.name}/ has no owner-gate DCO deferral doc"
        )

        files = parse_unified_diff(txt)
        assert files, f"{p.name}: no unified diff hunks found"
        for fentry in files:
            assert fentry["hunks"], f"{p.name}: diff entry {fentry['new_path']} has no hunks"
            for h in fentry["hunks"]:
                old_lines = sum(1 for line in h["body"] if line.startswith((" ", "-")))
                new_lines = sum(1 for line in h["body"] if line.startswith((" ", "+")))
                assert old_lines == h["old_count"]
                assert new_lines == h["new_count"]

        tree = work_dir / p.stem
        pflag = build_preimage_tree(tree, txt)
        chk = subprocess.run(
            ["git", "apply", pflag, "--check", str(p)],
            cwd=tree,
            env={**os.environ, "GIT_CEILING_DIRECTORIES": str(work_dir)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert chk.returncode == 0, f"{p.name} failed synthetic pre-image check: {chk.stderr}"

    combined = "\n".join(p.read_text(encoding="utf-8") for p in patches)
    assert "drivers/phy/rockchip/" in combined and "phy-rockchip-inno-usb2.c" in combined, (
        "port patch packet must target drivers/phy/rockchip/phy-rockchip-inno-usb2.c"
    )


def test_f5_t1_port_patches_format_patch_headers(tmp_path: Path) -> None:
    patches_dir = PORT_DIR / "patches"
    assert patches_dir.is_dir(), "M2 newos/port/patches/ missing"

    _validate_port_patches_dir(patches_dir, tmp_path)


# --- Tier 2: Boundary, Corner & Error Paths (6 tests) ---


def test_f5_t2_dts_u2phy_must_not_declare_phy_supply() -> None:
    for dts_path in (DTS_MAIN, DTS_MIN):
        stripped = _strip_dts_comments(dts_path.read_text(encoding="utf-8"))
        for node in ("&u2phy0", "&u2phy1"):
            if re.search(re.escape(node) + r"\s*\{", stripped):
                block = _extract_dts_block(stripped, re.escape(node))
                assert "phy-supply" not in block, f"{dts_path.name} {node} must not declare phy-supply"


def test_f5_t2_dts_i2s1_pinctrl_excludes_gpio0_b3_and_balanced_braces(tmp_path: Path) -> None:
    raw = DTS_MAIN.read_text(encoding="utf-8")
    assert "/dts-v1/;" in raw
    stripped = _strip_dts_comments(raw)
    assert stripped.count("{") == stripped.count("}")

    pinctrl = _extract_dts_block(stripped, r"&pinctrl")
    i2s1_pins = _extract_dts_block(pinctrl, r"i2s1_respeaker:\s*i2s1-respeaker")
    assert "RK_PB3" not in i2s1_pins, "i2s1_respeaker must omit GPIO0_B3 (LRCK_RX)"

    dtc = shutil.which("dtc")
    if dtc:
        # Run dtc syntax check only if dtc is installed on host
        res = subprocess.run(
            [dtc, "-I", "dts", "-O", "dtb", "-o", str(tmp_path / "out.dtb"), str(DTS_MAIN)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0 or "FATAL ERROR" not in res.stderr


def test_f5_t2_ac108_patch_rejects_already_applied_tree_b(tmp_path: Path) -> None:
    tree = tmp_path / "tree_b"
    shutil.copytree(TREE_B, tree)
    res = subprocess.run(
        ["git", "apply", "--check", str(AC108_PATCH)],
        cwd=tree,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0


def test_f5_t2_ac108_patch_reverse_apply_restores_tree_a(tmp_path: Path) -> None:
    tree = tmp_path / "tree_b"
    shutil.copytree(TREE_B, tree)
    res = subprocess.run(
        ["git", "apply", "-R", str(AC108_PATCH)],
        cwd=tree,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert not (tree / "sound" / "soc" / "codecs" / "ac108.c").exists()
    assert not (tree / "sound" / "soc" / "codecs" / "ac108.h").exists()
    for rel in ("sound/soc/codecs/Kconfig", "sound/soc/codecs/Makefile"):
        assert _sha256(tree / rel) == _sha256(TREE_A / rel)


def test_f5_t2_ac108_c_no_unconditional_chip_reset_in_probe() -> None:
    ac108_txt = (PORT_DIR / "ac108.c").read_text(encoding="utf-8")
    probe_body = _extract_dts_block(
        ac108_txt,
        r"static\s+int\s+ac108_(?:i2c_)?probe\s*\([^)]*\)",
    )
    assert "ac108_multi_chips_write(CHIP_AUDIO_RST" not in probe_body
    assert "ac108_multi_chips_write(CHIP_AUDIO_RST" not in ac108_txt
    assert "simple_strtol" not in ac108_txt


def test_f5_t2_ac108_patch_fails_on_corrupted_kconfig(tmp_path: Path) -> None:
    tree = tmp_path / "tree_bad"
    shutil.copytree(TREE_A, tree)
    kconfig = tree / "sound" / "soc" / "codecs" / "Kconfig"
    kconfig.write_text("corrupted kconfig\n", encoding="utf-8")

    res = subprocess.run(
        ["git", "apply", "--check", str(AC108_PATCH)],
        cwd=tree,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0


def test_f5_t2_port_patches_rejects_unsigned_without_owner_gate_and_broken_headers(tmp_path: Path) -> None:
    patches_dir = PORT_DIR / "patches"
    assert patches_dir.is_dir(), "M2 newos/port/patches/ missing"

    real_patch = next(iter(sorted(patches_dir.glob("*.patch"))))
    real_patch_txt = real_patch.read_text(encoding="utf-8")
    real_doc = next(iter(sorted(patches_dir.glob("*.md"))))
    real_doc_txt = real_doc.read_text(encoding="utf-8")

    # 1. Unsigned patch WITHOUT any owner-gate DCO doc must fail
    no_doc = tmp_path / "no_doc"
    no_doc.mkdir()
    (no_doc / real_patch.name).write_text(real_patch_txt, encoding="utf-8")
    with pytest.raises(AssertionError, match="Signed-off-by:"):
        _validate_port_patches_dir(no_doc, tmp_path / "w_no_doc")

    # 2. Unsigned patch WITH a doc lacking owner-gate/DCO rationale must fail
    weak_doc = tmp_path / "weak_doc"
    weak_doc.mkdir()
    (weak_doc / real_patch.name).write_text(real_patch_txt, encoding="utf-8")
    (weak_doc / real_doc.name).write_text("Note: Signed-off-by: was omitted.\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="Signed-off-by:"):
        _validate_port_patches_dir(weak_doc, tmp_path / "w_weak_doc")

    # 3. Broken format-patch Subject header must fail even with owner-gate doc
    bad_hdr = tmp_path / "bad_hdr"
    bad_hdr.mkdir()
    (bad_hdr / real_patch.name).write_text(
        real_patch_txt.replace("Subject: [PATCH", "Subject: broken", 1),
        encoding="utf-8",
    )
    (bad_hdr / real_doc.name).write_text(real_doc_txt, encoding="utf-8")
    with pytest.raises(AssertionError, match=r"Subject: \[PATCH"):
        _validate_port_patches_dir(bad_hdr, tmp_path / "w_bad_hdr")

    # 4. Broken diff --git header must fail
    bad_diff = tmp_path / "bad_diff"
    bad_diff.mkdir()
    (bad_diff / real_patch.name).write_text(
        real_patch_txt.replace("diff --git a/", "diff --git x/", 1),
        encoding="utf-8",
    )
    (bad_diff / real_doc.name).write_text(real_doc_txt, encoding="utf-8")
    with pytest.raises(AssertionError, match="diff --git"):
        _validate_port_patches_dir(bad_diff, tmp_path / "w_bad_diff")

    # 5. Wrong target file must fail
    wrong_target = tmp_path / "wrong_target"
    wrong_target.mkdir()
    (wrong_target / real_patch.name).write_text(
        real_patch_txt.replace("drivers/phy/rockchip/phy-rockchip-inno-usb2.c", "drivers/misc/other.c"),
        encoding="utf-8",
    )
    (wrong_target / real_doc.name).write_text(real_doc_txt, encoding="utf-8")
    with pytest.raises(AssertionError, match="phy-rockchip-inno-usb2.c"):
        _validate_port_patches_dir(wrong_target, tmp_path / "w_wrong_target")

    # 6. Properly signed patch passes without requiring an owner-gate deferral doc
    signed_no_doc = tmp_path / "signed_no_doc"
    signed_no_doc.mkdir()
    signed_txt = real_patch_txt.replace(
        "\n---\n",
        "\nSigned-off-by: Example Maintainer <maintainer@example.com>\n---\n",
        1,
    )
    (signed_no_doc / real_patch.name).write_text(signed_txt, encoding="utf-8")
    _validate_port_patches_dir(signed_no_doc, tmp_path / "w_signed_no_doc")
