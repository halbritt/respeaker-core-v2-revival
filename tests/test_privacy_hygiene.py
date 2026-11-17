"""F1: Public Repo Privacy & Git Hygiene verification tests."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from conftest import REPO_ROOT

EXPECTED_NOREPLY_EMAIL = "10265718+" + "hal" + "britt@users.noreply.github.com"
OWNER_HANDLE = "hal" + "britt"

# RFC 1918 plus the CGNAT block 100.64/10 (which Tailscale hands out). An address may end a
# sentence ("at <ip>."), so only a following digit or ".<digit>" makes it part of a longer
# dotted number.
LAN_IP_RE = re.compile(
    r"(?<![\d.])(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})(?!\d)(?!\.\d)"
)

# Mailboxes at consumer mail providers. "No personal email" is the rule; project, vendor
# and no-reply addresses are fine.
PERSONAL_MAIL_DOMAIN = (
    r"(?:gmail\.com|googlemail\.com|outlook\.com|hotmail\.com|live\.com|yahoo\.[a-z]{2,3}(?:\.[a-z]{2})?"
    r"|icloud\.com|me\.com|proton\.me|protonmail\.com|gmx\.[a-z]{2,3}|fastmail\.[a-z]{2,3})"
)
PERSONAL_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@" + PERSONAL_MAIL_DOMAIN + r"(?![A-Za-z0-9-])(?!\.[A-Za-z])", re.IGNORECASE
)
# Vendored kernel sources and patches carry their authors' addresses; those stay.
EMAIL_EXEMPT_PREFIXES = ("newos/vendor-4.4/", "newos/port/")

# Credential forms. Each captures the value in group "val"; _is_placeholder() decides
# whether that value is a literal.
_VAL = r"""(?P<val>"[^"]*"|'[^']*'|\S+)"""
CREDENTIAL_RES = (
    ("password", re.compile(r"(?i)(?<![A-Za-z0-9])(?:pass(?:word|wd|phrase)?|pw)\s*[:=]\s*" + _VAL)),
    ("sshpass -p", re.compile(r"\bsshpass\s+-p\s*" + _VAL)),
    ("token", re.compile(r"\b[A-Z0-9_]*TOKEN\s*=\s*" + _VAL)),
    ("--token", re.compile(r"(?<![\w-])--token(?:\s+|=)" + _VAL)),
    ("bearer", re.compile(r"""(?i)\bAuthorization["']?\s*[:=]\s*["']?Bearer\s+(?P<val>[^\s"']*)""")),
)
# Values that are plainly not secrets: test fixtures in this repo use these prefixes.
_FIXTURE_VALUE_RE = re.compile(r"(?i)^(?:test|dummy|example|fake|wrong|changeme|placeholder)(?![a-z])")

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


def find_personal_emails(text: str) -> list[tuple[int, str]]:
    """Find addresses at personal mail providers."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in PERSONAL_EMAIL_RE.finditer(line):
            hits.append((lineno, m.group(0)))
    return hits


def _is_placeholder(raw_val: str) -> bool:
    """True if a captured credential value is a variable, a placeholder or a test fixture
    rather than a literal secret."""
    val = raw_val.strip().strip("\"'").rstrip(",;.)\"'")
    if not val or val in {"...", "…"} or set(val) <= set("xX*.-_"):
        return True
    if val[0] in "$<{%[\\`/" or "$" in val or "(" in val:      # variables, placeholders, sed/regex text
        return True
    if re.fullmatch(r"[A-Z][A-Z_]*", val):          # metavariable in usage text: --token TOKEN
        return True
    return bool(_FIXTURE_VALUE_RE.match(val))


def find_credentials(text: str) -> list[tuple[int, str]]:
    """Find literal passwords and tokens in the forms of CREDENTIAL_RES: a password or
    token assignment, an sshpass argument, a token option, a bearer header."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, rx in CREDENTIAL_RES:
            for m in rx.finditer(line):
                if re.search(r"\$\{\w*$", line[: m.start()]):      # shell ${VAR:?msg}, ${VAR:-default}
                    continue
                if not _is_placeholder(m.group("val")):
                    hits.append((lineno, f"{kind}: {m.group(0)[:60]}"))
    return hits


PRIVATE_TERMS_ENV = "RSP_PRIVATE_TERMS"
PRIVATE_TERMS_DEFAULT = REPO_ROOT / "local" / "private-terms.txt"


def private_terms_file() -> Path | None:
    """The private denylist: $RSP_PRIVATE_TERMS, else local/private-terms.txt (gitignored).
    Hostnames and user names that must not be published cannot be listed in this file."""
    named = os.environ.get(PRIVATE_TERMS_ENV)
    path = Path(named) if named else PRIVATE_TERMS_DEFAULT
    return path if path.is_file() else None


def load_private_terms(path: Path) -> list[str]:
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.split("#", 1)[0].strip()
        if term:
            terms.append(term)
    return terms


def find_private_terms(text: str, terms: list[str]) -> list[tuple[int, str]]:
    """Whole-word, case-insensitive hits of denylisted terms. A term may appear as the
    GitHub owner (github.com/<term>) and inside a GitHub no-reply address; nowhere else."""
    hits: list[tuple[int, str]] = []
    if not terms:
        return hits
    compiled = []
    for term in terms:
        t = re.escape(term)
        allowed = re.compile(
            rf"github\.com[/:]{t}(?![A-Za-z0-9])|\d+\+{t}@users\.noreply\.github\.com", re.IGNORECASE
        )
        compiled.append((term, allowed, re.compile(rf"(?<![A-Za-z0-9]){t}(?![A-Za-z0-9])", re.IGNORECASE)))
    for lineno, line in enumerate(text.splitlines(), 1):
        for term, allowed, rx in compiled:
            if rx.search(allowed.sub("", line)):
                hits.append((lineno, f"private term {term!r}: {line.strip()[:100]}"))
    return hits


def scan_text(text: str, rel: str = "", terms: list[str] | None = None) -> list[tuple[int, str]]:
    """Every privacy rule applied to one text. `rel` is the repo-relative path, used for
    the vendored-source e-mail exemption; empty for text that is not a file."""
    hits = (
        find_lan_ips(text)
        + find_private_hostnames(text)
        + find_wifi_or_token_secrets(text)
        + find_owner_leaks(text)
        + find_credentials(text)
    )
    if not rel.startswith(EMAIL_EXEMPT_PREFIXES):
        hits += find_personal_emails(text)
    if terms:
        hits += find_private_terms(text, terms)
    return sorted(hits)


def scan_repo_files(repo: Path = REPO_ROOT, terms: list[str] | None = None, only_terms: bool = False) -> list[str]:
    leaks: list[str] = []
    for path in list_repo_files(repo):
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = path.relative_to(repo).as_posix()
        found = find_private_terms(text, terms or []) if only_terms else scan_text(text, rel, terms)
        leaks.extend(f"{rel}:{lineno}: {what}" for lineno, what in found)
    return leaks


def is_git_checkout(repo: Path = REPO_ROOT) -> bool:
    """True if `repo` is the top of a git work tree and git is installed."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except OSError:
        return False
    return res.returncode == 0 and Path(res.stdout.strip()).resolve() == repo.resolve()


def require_git_checkout(repo: Path = REPO_ROOT) -> None:
    if not is_git_checkout(repo):
        pytest.skip(f"{repo} is not a git checkout (or git is not installed): no commit history to check")


def commit_messages(repo: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """(short hash, full message) of every commit reachable from HEAD."""
    res = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%h%x01%B%x00"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    out: list[tuple[str, str]] = []
    for chunk in res.stdout.split("\x00"):
        if "\x01" in chunk:
            sha, body = chunk.strip("\n").split("\x01", 1)
            out.append((sha, body))
    return out


def scan_commit_messages(repo: Path = REPO_ROOT, terms: list[str] | None = None, only_terms: bool = False) -> list[str]:
    leaks: list[str] = []
    for sha, body in commit_messages(repo):
        found = find_private_terms(body, terms or []) if only_terms else scan_text(body, "", terms)
        leaks.extend(f"commit {sha}:{lineno}: {what}" for lineno, what in found)
    return leaks


def personal_commit_emails(repo: Path = REPO_ROOT) -> list[str]:
    """Author and committer addresses in the history that belong to a personal mail provider."""
    res = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%h %ae%n%h %ce"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    bad = {line.strip() for line in res.stdout.splitlines() if PERSONAL_EMAIL_RE.fullmatch(line.split(" ", 1)[-1].strip())}
    return sorted(bad)


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


def test_f1_t1_no_personal_email_in_commit_authors() -> None:
    """Any contributor identity is fine except a mailbox at a personal mail provider."""
    require_git_checkout()
    assert personal_commit_emails() == []


def test_f1_t1_no_personal_emails_in_tracked_files() -> None:
    leaks: list[str] = []
    for path in list_repo_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = read_text_if_not_binary(path)
        if text is None or rel.startswith(EMAIL_EXEMPT_PREFIXES):
            continue
        leaks.extend(f"{rel}:{lineno}: {addr}" for lineno, addr in find_personal_emails(text))
    assert not leaks, f"Personal e-mail addresses found: {leaks}"


def test_f1_t1_no_literal_credentials_in_tracked_files() -> None:
    leaks: list[str] = []
    for path in list_repo_files():
        text = read_text_if_not_binary(path)
        if text is None:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        leaks.extend(f"{rel}:{lineno}: {what}" for lineno, what in find_credentials(text))
    assert not leaks, f"Literal passwords or tokens found: {leaks}"


def test_f1_t1_commit_messages_pass_the_same_privacy_rules() -> None:
    require_git_checkout()
    leaks = scan_commit_messages()
    assert not leaks, f"Privacy leaks in commit messages: {leaks}"


def test_f1_t1_private_denylist_terms_absent_from_files_and_commit_messages() -> None:
    """Site-specific names (hosts, user names) come from a private, untracked list."""
    terms_file = private_terms_file()
    if terms_file is None:
        pytest.skip(
            f"no private denylist: put one term per line in {PRIVATE_TERMS_DEFAULT.relative_to(REPO_ROOT)} "
            f"(gitignored) or name a file in ${PRIVATE_TERMS_ENV}"
        )
    terms = load_private_terms(terms_file)
    if not terms:
        pytest.skip(f"private denylist {terms_file} lists no terms")
    leaks = scan_repo_files(terms=terms, only_terms=True)
    if is_git_checkout():
        leaks += scan_commit_messages(terms=terms, only_terms=True)
    assert not leaks, "Private terms found:\n" + "\n".join(leaks)


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
            "STT_HTTP_TOK" + "EN=" + "tok" + "123",
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


def _git(repo: Path, *args: str, email: str = "dev@example.org") -> None:
    """Run git in a throwaway repo with an explicit identity (the user's git config is not read)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_EMAIL": email,
    }
    subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, timeout=30, check=True)


def _scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_f1_t2_tracked_credentials_file_is_listed(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path)
    (repo / "board-credentials.txt").write_text("WIFI_PSK" + "=dummy_secret\n", encoding="utf-8")
    _git(repo, "add", "board-credentials.txt")
    assert "board-credentials.txt" in {p.name for p in list_repo_files(repo)}


def test_f1_t2_commit_email_rule_flags_personal_providers_only(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path)
    (repo / "f").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "one", email="1234+someone@users.noreply.github.com")
    (repo / "f").write_text("2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "two", email="maintainer@example.org")
    assert personal_commit_emails(repo) == []

    (repo / "f").write_text("3\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "three", email="some.body@" + "gmail" + ".com")
    bad = personal_commit_emails(repo)
    assert len(bad) == 1 and bad[0].endswith("@" + "gmail" + ".com")


def test_f1_t2_commit_checks_skip_outside_a_git_checkout(tmp_path: Path) -> None:
    assert is_git_checkout(tmp_path) is False
    with pytest.raises(pytest.skip.Exception, match="not a git checkout"):
        require_git_checkout(tmp_path)


@pytest.mark.parametrize(
    ("octets", "should_flag"),
    [
        (("100", "64", "0", "1"), True),
        (("100", "100", "20", "30"), True),
        (("100", "127", "255", "254"), True),
        (("100", "63", "255", "254"), False),
        (("100", "128", "0", "1"), False),
    ],
)
def test_f1_t2_detects_cgnat_range(octets: tuple[str, ...], should_flag: bool) -> None:
    assert bool(find_lan_ips("peer " + ".".join(octets) + " up")) is should_flag


def test_f1_t2_detects_ip_at_end_of_sentence_and_in_punctuation() -> None:
    ip = ".".join(["10", "0", "0", "12"])
    for line in (f"The board is at {ip}.", f"({ip})", f"ssh {ip}, then wait", f"{ip}:22", f'"{ip}"'):
        assert find_lan_ips(line) == [(1, ip)], line
    # a longer dotted number is not an address
    assert find_lan_ips("version " + ip + ".7") == []


@pytest.mark.parametrize(
    "domain",
    ["gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "yahoo.co.uk",
     "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net", "fastmail.com", "fastmail.fm"],
)
def test_f1_t2_detects_personal_provider_addresses(domain: str) -> None:
    addr = "first.last+tag" + "@" + domain
    assert find_personal_emails(f"Contact: <{addr}>") == [(1, addr)]
    assert find_personal_emails(f"mail {addr.upper()}.") == [(1, addr.upper())]


def test_f1_t2_allows_project_and_noreply_addresses() -> None:
    at = "@"
    ok = "\n".join(
        [
            f"Signed-off-by: Maintainer <maintainer{at}kernel.org>",
            f"dev{at}example.com",
            EXPECTED_NOREPLY_EMAIL,
            f"someone{at}me.community",          # not me.com
            f"x{at}notgmail.com",
            f"x{at}gmail.com.example.org",
        ]
    )
    assert find_personal_emails(ok) == []


def test_f1_t2_vendored_sources_keep_author_emails_but_other_paths_do_not() -> None:
    line = "Author: Some Dev <somedev" + "@" + "gmail.com>\n"
    assert scan_text(line, "newos/port/patches/0001-x.patch") == []
    assert scan_text(line, "newos/vendor-4.4/ac108.c") == []
    assert len(scan_text(line, "README.md")) == 1
    assert len(scan_text(line, "")) == 1                      # commit messages


def _cred_samples() -> dict[str, str]:
    """One planted violation per credential form, assembled so this file stays clean."""
    secret = "S3" + "cr3t" + "Valu3"
    return {
        "password": "pass" + "word: " + secret,
        "password assigned": "PASS" + "WORD=" + secret,
        "PW": "P" + "W=" + secret,
        "sshpass": "ssh" + "pass -p " + secret + " ssh board",
        "RSP_TOKEN": "RSP_TOK" + "EN=" + secret,
        "RSP_TOKEN quoted": 'export RSP_TOK' + 'EN="' + secret + '"',
        "--token": "rsp-listen --tok" + "en " + secret + " --wake x",
        "token option assigned": "rsp-listen --tok" + "en=" + secret,
        "bearer": "Authori" + "zation: Bearer " + secret,
        "bearer header dict": '{"Authori' + 'zation": "Bearer ' + secret + '"}',
    }


@pytest.mark.parametrize("form", sorted(_cred_samples()))
def test_f1_t2_detects_each_literal_credential_form(form: str) -> None:
    hits = find_credentials("# setup\n" + _cred_samples()[form] + "\n")
    assert len(hits) == 1 and hits[0][0] == 2, hits


def test_f1_t2_credential_placeholders_and_code_are_not_flagged() -> None:
    k = {
        "pw": "pass" + "word",
        "PW": "P" + "W",
        "sp": "ssh" + "pass -p",
        "tok": "RSP_TOK" + "EN",
        "opt": "--tok" + "en",
        "auth": "Authori" + "zation: Bearer",
    }
    good = "\n".join(
        [
            f"{k['pw']}: <board password>",
            f"{k['pw']}: $BOARD_PW",
            f'{k["PW"]}="$1"',
            k["PW"] + "=${BOARD_" + k["PW"] + ":?set it}",
            "${BOARD_" + f"{k['PW']}" + ":?set it}",
            f"{k['PW']}=$(sed -n 's/^{k['pw']}: //p' creds.txt)",
            f"{k['PW']}=$(cat ../board-credentials.txt)",
            f"{k['PW']}_HASH=$(awk x)",
            f'{k["sp"]} "$PW" ssh board',
            f"{k['sp']} ... ssh board",
            f"{k['tok']}=<token>",
            f"{k['tok']}=${{TOKEN}}",
            f'{k["tok"]}="..."',
            f"#{k['tok']}=",
            f"{k['opt']} <token>",
            f"{k['opt']} TOKEN",
            f'{k["opt"]} "$RSP_TOKEN"',
            f"{k['opt']} test-token",
            f"{k['auth']} <token>",
            f"{k['auth']} $TOKEN",
            f'"{k["auth"]} " + HTTP_TOKEN',
            'HTTP_TOKEN = env("STT_HTTP_TOKEN", "")',
            "PasswordAuthentication no",
        ]
    )
    assert find_credentials(good) == []


def test_f1_t2_private_terms_whole_word_case_insensitive_with_github_owner_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("# site names\nboxname   # the workstation\n\nownerx\n", encoding="utf-8")
    monkeypatch.setenv(PRIVATE_TERMS_ENV, str(terms_file))
    assert private_terms_file() == terms_file
    terms = load_private_terms(terms_file)
    assert terms == ["boxname", "ownerx"]

    for bad in ("ssh boxname", "Host BoxName.", "/home/ownerx/src", "scp x ownerx-pc:", "user=ownerx_admin",
                "gh repo clone ownerx/project", "ownerx@example.org"):
        assert len(find_private_terms(bad, terms)) == 1, bad
    for good in ("the sandboxname is fine", "boxnames", "https://github.com/ownerx/project",
                 "git@github.com:ownerx/project.git", "Dev <1234+ownerx@users.noreply.github.com>"):
        assert find_private_terms(good, terms) == [], good
    # the allowed contexts do not hide another occurrence on the same line
    assert len(find_private_terms("https://github.com/ownerx/project cloned to /home/ownerx", terms)) == 1


def test_f1_t2_private_terms_file_absent_means_no_denylist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PRIVATE_TERMS_ENV, str(tmp_path / "missing.txt"))
    assert private_terms_file() is None


def test_f1_t2_scanners_find_planted_violations_in_files_and_commit_messages(tmp_path: Path) -> None:
    """End to end over a scratch repository: one planted violation of each kind, in a
    tracked file and in a commit message, is reported with its location."""
    repo = _scratch_repo(tmp_path)
    ip = ".".join(["10", "0", "0", "12"])
    cgnat = ".".join(["100", "101", "102", "103"])
    mail = "jane.doe" + "@" + "proton.me"
    cred = _cred_samples()["sshpass"]
    (repo / "README.md").write_text(
        f"clean line\nThe board is at {ip}.\ntailnet {cgnat}\nmail {mail}\n{cred}\nlogin on boxname\n",
        encoding="utf-8",
    )
    (repo / "clean.txt").write_text("nothing here, see https://github.com/ownerx/project\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add notes\n\ntested against {ip}.\nreported by {mail}\nrun from boxname")

    files = scan_repo_files(repo, terms=["boxname", "ownerx"])
    assert [f.split(": ", 1)[0] for f in files] == [f"README.md:{n}" for n in (2, 3, 4, 5, 6)], files
    msgs = scan_commit_messages(repo, terms=["boxname", "ownerx"])
    assert sorted(m.split(":")[1] for m in msgs) == ["3", "4", "5"], msgs
    assert all(m.startswith("commit ") for m in msgs)

    assert scan_repo_files(repo, terms=["boxname"], only_terms=True) == [
        "README.md:6: private term 'boxname': login on boxname"
    ]
