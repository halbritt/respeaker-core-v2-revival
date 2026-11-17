"""F1: Public Repo Privacy & Git Hygiene verification tests."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT

EXPECTED_NOREPLY_EMAIL = "10265718+" + "hal" + "britt@users.noreply.github.com"
OWNER_HANDLE = "hal" + "britt"
PRIVATE_SITE = "fern" + "side"

LAN_IP_RE = re.compile(
    r"(?<![\d.])(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])"
)

HOSTNAME_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:local|lan|home|internal|corp|home\.arpa|ts\.net))\b",
    re.IGNORECASE,
)

SECRET_ASSIGN_RE = re.compile(
    r"(?im)^[^#\n]*?\b(WIFI_SSID|WIFI_PSK|STT_HTTP_TOKEN|wpa_passphrase|psk|ssid)\s*[:=]\s*([^\s#;]+)"
)

ALLOWED_LOCAL_FILENAMES = {"rsp-stt.local"}


def list_repo_files(repo: Path = REPO_ROOT) -> list[Path]:
    """Return tracked and untracked non-ignored files via `git ls-files`."""
    res = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    )
    paths: list[Path] = []
    for raw in res.stdout.split(b"\x00"):
        if not raw:
            continue
        p = repo / raw.decode("utf-8", errors="replace")
        if p.is_file():
            paths.append(p)
    return paths


def read_text_if_not_binary(path: Path) -> str | None:
    """Return UTF-8 text for non-binary files, or None for binary files."""
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def find_lan_ips(text: str) -> list[tuple[int, str]]:
    """Find RFC 1918 private IPv4 addresses with valid 0..255 octets."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in LAN_IP_RE.finditer(line):
            ip = m.group(0)
            octets = [int(o) for o in ip.split(".")]
            if all(0 <= o <= 255 for o in octets):
                hits.append((lineno, ip))
    return hits


def find_private_hostnames(text: str) -> list[tuple[int, str]]:
    """Find private LAN/mDNS hostnames while allowing config file suffixes like rsp-stt.local."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in HOSTNAME_RE.finditer(line):
            host = m.group(1)
            if host.lower() in ALLOWED_LOCAL_FILENAMES:
                continue
            prefix = line[: m.start()]
            token_before = re.split(r"[\s=\"']", prefix)[-1]
            if "://" in token_before:
                if "/" in token_before.split("://", 1)[1]:
                    continue
            elif "/" in token_before:
                continue
            hits.append((lineno, host))
        if re.search(rf"\b{PRIVATE_SITE}\b", line, re.IGNORECASE):
            hits.append((lineno, PRIVATE_SITE))
    return hits


def find_wifi_or_token_secrets(text: str) -> list[tuple[int, str]]:
    """Find literal Wi-Fi SSID/PSK or HTTP token assignments (excluding vars/placeholders)."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in SECRET_ASSIGN_RE.finditer(line):
            key, raw_val = m.group(1), m.group(2)
            val = raw_val.strip().strip('"').strip("'")
            if not val:
                continue
            if val.startswith(("$", "<", "{", "//")):
                continue
            if key.lower() in {"ssid", "psk"} and val.endswith(")"):
                continue
            hits.append((lineno, f"{key}={raw_val}"))
    return hits


def find_owner_leaks(text: str) -> list[tuple[int, str]]:
    """Find leaked owner local paths or bare handles outside allowlisted GitHub noreply/URLs."""
    hits: list[tuple[int, str]] = []
    noreply_re = re.compile(re.escape(EXPECTED_NOREPLY_EMAIL), re.IGNORECASE)
    gh_url_re = re.compile(rf"https?://github\.com/{OWNER_HANDLE}(?:/|\b)", re.IGNORECASE)
    owner_re = re.compile(OWNER_HANDLE, re.IGNORECASE)

    for lineno, line in enumerate(text.splitlines(), 1):
        if noreply_re.search(line) and re.match(r"^(?:From|Author|Commit|Signed-off-by):", line.strip(), re.I):
            continue
        sanitized = noreply_re.sub("", line)
        sanitized = gh_url_re.sub("", sanitized)
        if owner_re.search(sanitized):
            hits.append((lineno, line.strip()[:120]))
    return hits


# --- Tier 1: Happy-Path Repository Invariants (6 tests) ---


def test_f1_t1_no_lan_ips_in_tracked_files() -> None:
    leaks: list[str] = []
    for path in list_repo_files():
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = path.relative_to(REPO_ROOT)
        for lineno, ip in find_lan_ips(text):
            leaks.append(f"{rel}:{lineno}: {ip}")
    assert not leaks, f"RFC 1918 LAN IPs found: {leaks}"


def test_f1_t1_allowlisted_ips_and_examples_permitted() -> None:
    sample_lines = "\n".join(
        [
            "STT_SERVER=http://whisper.example:8910/inference",
            "STT_HTTP=127.0.0.1:8911",
            "BIND=0.0.0.0:8911",
            "DOC_A=192.0.2.10",
            "DOC_B=198.51.100.5",
            "DOC_C=203.0.113.42",
            "EnvironmentFile=-/etc/default/rsp-stt.local",
        ]
    )
    assert find_lan_ips(sample_lines) == []
    assert find_private_hostnames(sample_lines) == []
    # Confirm all non-binary tracked/non-ignored repo files are free of private hostnames
    host_leaks: list[str] = []
    for path in list_repo_files():
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = path.relative_to(REPO_ROOT)
        for lineno, host in find_private_hostnames(text):
            host_leaks.append(f"{rel}:{lineno}: {host}")
    assert not host_leaks, f"Private hostnames found: {host_leaks}"


def test_f1_t1_no_wifi_or_token_secrets_in_tracked_files() -> None:
    leaks: list[str] = []
    for path in list_repo_files():
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = path.relative_to(REPO_ROOT)
        for lineno, match in find_wifi_or_token_secrets(text):
            leaks.append(f"{rel}:{lineno}: {match}")
    assert not leaks, f"Literal Wi-Fi or HTTP token secrets found: {leaks}"


def test_f1_t1_no_owner_username_or_home_path_leaks() -> None:
    leaks: list[str] = []
    for path in list_repo_files():
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for lineno, snippet in find_owner_leaks(text):
            leaks.append(f"{rel}:{lineno}: {snippet}")
    assert not leaks, f"Owner handle/path leaks found: {leaks}"


def test_f1_t1_git_author_email_is_github_noreply() -> None:
    cfg = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "config", "--get", "user.email"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert cfg.stdout.strip() == EXPECTED_NOREPLY_EMAIL

    log = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "--format=%ae%n%ce"],
        capture_output=True,
        text=True,
        check=True,
    )
    emails = {line.strip() for line in log.stdout.splitlines() if line.strip()}
    assert emails == {EXPECTED_NOREPLY_EMAIL}


def test_f1_t1_gitignore_and_licenses_cover_sensitive_artifacts() -> None:
    expected_ignored = [
        "board-credentials.txt",
        "images/",
        "newos/cross/src/",
        "newos/cross/out/",
        "newos/out/",
        "newos/armbian-build/",
        "newos/debug-initrd/logs/",
        "__pycache__/",
        "local/",
    ]
    for pattern in expected_ignored:
        res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", pattern],
            check=False,
        )
        assert res.returncode == 0, f"{pattern} is not ignored by .gitignore"

    tracked = {str(p.relative_to(REPO_ROOT)) for p in list_repo_files()}
    assert "board-credentials.txt" not in tracked
    assert (REPO_ROOT / "LICENSE").is_file() and (REPO_ROOT / "LICENSE").stat().st_size > 0
    gpl = REPO_ROOT / "LICENSES" / "GPL-2.0-only.txt"
    assert gpl.is_file() and gpl.stat().st_size > 0


# --- Tier 2: Boundary, Negative Detection & Edge Cases (6 tests) ---


@pytest.mark.parametrize(
    ("candidate_ip", "should_flag"),
    [
        (".".join(["10", "0", "0", "1"]), True),
        (".".join(["10", "255", "255", "254"]), True),
        (".".join(["192", "168", "0", "1"]), True),
        (".".join(["192", "168", "255", "254"]), True),
        (".".join(["172", "16", "0", "1"]), True),
        (".".join(["172", "31", "255", "254"]), True),
        (".".join(["172", "15", "255", "254"]), False),
        (".".join(["172", "32", "0", "1"]), False),
        (".".join(["192", "169", "1", "1"]), False),
        (".".join(["1", "10", "0", "1"]), False),
        (".".join(["999", "168", "1", "1"]), False),
    ],
)
def test_f1_t2_detects_rfc1918_boundary_ips(candidate_ip: str, should_flag: bool) -> None:
    hits = find_lan_ips(f"STT_SERVER=http://{candidate_ip}:8910/inference")
    assert bool(hits) is should_flag


def test_f1_t2_detects_private_hostnames_without_file_suffix_fp() -> None:
    bad_1 = "http://" + "mybox" + ".local:8910/inference"
    bad_2 = "ssh " + "host" + ".lan"
    bad_3 = "whisper." + "home.arpa"
    bad_userinfo = "https://user:pass@" + "whisper" + ".lan/api"
    good = "/etc/default/rsp-stt" + ".local\nhttp://whisper.example:8910/inference\nhttps://example.com/static/doc.local"
    assert len(find_private_hostnames(bad_1)) == 1
    assert len(find_private_hostnames(bad_2)) == 1
    assert len(find_private_hostnames(bad_3)) == 1
    assert len(find_private_hostnames(bad_userinfo)) == 1
    assert find_private_hostnames(good) == []


def test_f1_t2_detects_literal_wifi_psk_and_token_allows_placeholders() -> None:
    bad = "\n".join(
        [
            "WIFI_PSK=" + "super" + "secret",
            'psk="' + "hun" + 'ter2"',
            "STT_HTTP_TOKEN=" + "tok" + "123",
        ]
    )
    good = "\n".join(
        [
            'psk="$PSK"',
            "WIFI_SSID=<network>",
            "#STT_HTTP_TOKEN=",
            "PSK=${WIFI_PSK:-$(sed -n 's/^psk=//p' \"$WIFI_NM\")}",
        ]
    )
    assert len(find_wifi_or_token_secrets(bad)) == 3
    assert find_wifi_or_token_secrets(good) == []


def test_f1_t2_detects_owner_home_path_allows_noreply_and_license() -> None:
    bad_home = "/home/" + OWNER_HANDLE + "/.ssh/id_ed25519"
    bad_host = "STT_HOST=" + OWNER_HANDLE + "-pc"
    good_license = "Copyright (c) 2026 Heath Albritton"
    good_trailer = f"Signed-off-by: Owner <{EXPECTED_NOREPLY_EMAIL}>"
    good_url = "https://github.com/" + OWNER_HANDLE + "/respeaker-core-v2-revival"
    assert len(find_owner_leaks(bad_home)) == 1
    assert len(find_owner_leaks(bad_host)) == 1
    assert find_owner_leaks(good_license) == []
    assert find_owner_leaks(good_trailer) == []
    assert find_owner_leaks(good_url) == []


def test_f1_t2_binary_files_handled_cleanly_without_crash(tmp_path: Path) -> None:
    dtb = REPO_ROOT / "newos" / "vendor-4.4" / "rk3229-respeaker-v2.dtb"
    assert dtb.is_file()
    assert read_text_if_not_binary(dtb) is None

    synthetic_bin = tmp_path / "blob.bin"
    synthetic_bin.write_bytes(b"\x00\xff\xfe\x80" + (".".join(["192", "168", "1", "1"])).encode())
    assert read_text_if_not_binary(synthetic_bin) is None


def test_f1_t2_rejects_non_noreply_git_email_and_tracked_credentials(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "personal@example.com"], check=True)
    creds = repo / "board-credentials.txt"
    creds.write_text("WIFI_PSK" + "=dummy_secret\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "board-credentials.txt"], check=True)

    email = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "user.email"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert email != EXPECTED_NOREPLY_EMAIL
    tracked = {p.name for p in list_repo_files(repo)}
    assert "board-credentials.txt" in tracked
