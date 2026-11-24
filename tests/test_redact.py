"""Redaction tests. Secret-looking test values are assembled at run time so no
real-looking token appears literally in the repository."""

import pytest

from vast_troubleshooter.redact import (
    ID,
    IP,
    KEY,
    SECRET,
    Redactor,
    local_identifiers,
)

R = Redactor(
    hostnames=["gpu-rig-07.corp.example.com"],
    usernames=["alice"],
    machine_ids=["0123456789abcdef0123456789abcdef"],
)

HF = "hf_" + "x" * 34
GH = "ghp_" + "y" * 36
SK = "sk-" + "z" * 30
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
PEM = (
    "-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA\nabcdefg\n"
    "-----END " + "RSA PRIVATE KEY-----"
)


def gone(sample: str, *needles: str) -> None:
    out = R.text(sample)
    for needle in needles:
        assert needle not in out, f"{needle!r} survived in {out!r}"


# -- addresses ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.5",
        "192.168.1.20",
        "203.0.113.7",
        "172.16.0.1",
        "0.0.0.0",
        "255.255.255.255",
    ],
)
def test_ipv4_addresses_are_removed(ip: str) -> None:
    for sample in (
        f"connect to {ip} failed",
        f"{ip}:2375",
        f"{ip}/24",
        f"via {ip}, dev eth0",
    ):
        gone(sample, ip)
        assert IP in R.text(sample)


@pytest.mark.parametrize(
    "ip",
    [
        "2001:db8::1",
        "2606:4700:4700::1111",
        "fe80::1",
        "::1",
        "2001:0db8:0000:0000:0000:ff00:0042:8329",
        "fe80::a00:27ff:fe4e:66a1%eth0",
    ],
)
def test_ipv6_addresses_are_removed(ip: str) -> None:
    gone(f"address {ip} up", ip.split("%")[0])
    assert IP in R.text(f"address {ip} up")


def test_ipv4_mapped_ipv6_does_not_leak_the_v4_part() -> None:
    gone("::ffff:192.0.2.1", "192.0.2.1")


@pytest.mark.parametrize(
    "text",
    [
        "NVIDIA driver 595.91.07",
        "driver 580.178.04 CUDA 13.0",
        "kernel 6.8.0-45-generic",
        "Ubuntu 24.04.1 LTS",
        "time 2026-11-23T10:15:42+00:00",
        "at 10:15:42 and 12:00:00",
        "PCI 0000:3a:00.0",
        "cuda_version_code 11080",
        "256.1.1.1 is not an address",
        "1.2.3 and 12.34",
    ],
)
def test_versions_times_and_pci_ids_are_not_mistaken_for_addresses(text: str) -> None:
    assert R.text(text) == text


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"])
def test_well_known_public_resolvers_stay_readable(ip: str) -> None:
    assert (
        R.text(f"try a public resolver such as {ip}")
        == f"try a public resolver such as {ip}"
    )


def test_other_public_addresses_are_still_removed() -> None:
    gone("resolver 1.1.1.2", "1.1.1.2")


def test_mac_addresses_are_removed() -> None:
    gone("link 00:1a:2b:3c:4d:5e up", "00:1a:2b:3c:4d:5e")
    gone("link 00-1A-2B-3C-4D-5E up", "00-1A-2B-3C-4D-5E")


# -- secrets ------------------------------------------------------------------------------


@pytest.mark.parametrize("token", [HF, GH, SK, AWS, JWT])
def test_known_token_formats_are_removed(token: str) -> None:
    gone(f"found {token} here", token)
    assert KEY in R.text(f"found {token} here")


@pytest.mark.parametrize(
    "text",
    [
        "api_key=abc123def",
        "API-KEY: abc123def",
        "password: hunter2",
        "DB_PASSWORD=hunter2",
        'token="abc def ghi"',
        "secret='top secret'",
        "X-Auth-Token: abcdef123456",
        "export HF_TOKEN=abcdef123456",
        "passwd=hunter2",
        "Authorization: Bearer abcdefgh12345678",
    ],
)
def test_key_value_secrets_are_removed(text: str) -> None:
    out = R.text(text)
    for secret in (
        "abc123def",
        "hunter2",
        "abc def ghi",
        "top secret",
        "abcdef123456",
        "abcdefgh12345678",
    ):
        assert secret not in out
    assert SECRET in out


def test_private_key_blocks_are_removed_even_when_truncated() -> None:
    gone(f"before\n{PEM}\nafter", "MIIEowIBAAKCAQEA", "abcdefg")
    truncated = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1yc2E\nmore"
    gone(truncated, "AAAAB3NzaC1yc2E")
    assert "before" in R.text(f"before\n{PEM}\nafter") and "after" in R.text(
        f"before\n{PEM}\nafter"
    )


def test_emails_uuids_and_long_ids_are_removed() -> None:
    gone("contact bob@example.org please", "bob@example.org")
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    gone(f"instance {uuid}", uuid)
    long_hex = "ab12" * 10
    gone(f"id {long_hex}", long_hex)
    assert ID in R.text(f"id {long_hex}")


def test_long_mixed_opaque_strings_are_removed_but_plain_words_and_paths_are_not() -> (
    None
):
    opaque = "Qk9PTVRoaXNJc0Ew" + "NjRFbmNvZGVkU2VjcmV0" + "V2l0aERpZ2l0czEyMw"
    gone(f"blob {opaque}", opaque)
    assert (
        R.text("/var/lib/vastai_kaalia/kaalia.log")
        == "/var/lib/vastai_kaalia/kaalia.log"
    )
    assert (
        R.text("nvidia-container-toolkit-base-installed-ok")
        == "nvidia-container-toolkit-base-installed-ok"
    )


# -- this host's own identity ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "host gpu-rig-07.corp.example.com is down",
        "host GPU-RIG-07.CORP.EXAMPLE.COM is down",
        "host gpu-rig-07 is down",
        "ssh root@gpu-rig-07",
    ],
)
def test_the_hosts_own_name_is_removed_in_every_form(text: str) -> None:
    gone(text, "gpu-rig-07")


def test_the_machine_id_is_removed_wherever_it_appears() -> None:
    gone(
        "machine 0123456789abcdef0123456789abcdef ok",
        "0123456789abcdef0123456789abcdef",
    )
    gone("id=0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef")


@pytest.mark.parametrize(
    "text",
    [
        "/home/alice/.ssh/id_rsa",
        "/home/alice",
        "C:\\Users\\alice\\AppData\\x",
        "/Users/alice/Library",
        "user alice logged in",
        "owner=ALICE",
    ],
)
def test_user_names_are_removed(text: str) -> None:
    gone(text, "alice", "ALICE")


def test_other_users_home_paths_are_removed_too() -> None:
    assert R.text("/home/carol/x") == "/home/[user]/x"
    assert R.text("C:\\Users\\carol\\x") == "C:\\Users\\[user]\\x"


def test_a_name_inside_another_word_is_left_alone_and_short_names_are_ignored() -> None:
    assert R.text("a malicious actor") == "a malicious actor"
    short = Redactor(usernames=["ab"], hostnames=["x"])
    assert short.text("ab x cabx") == "ab x cabx"  # too short to redact safely


# -- behavior ---------------------------------------------------------------------------------------


SAMPLES = [
    f"ip 10.0.0.5 host gpu-rig-07 user alice token={HF}",
    f"{PEM}\nafter 192.168.1.1",
    "api_key=abc password: x /home/alice/x 2001:db8::1",
    "plain text with nothing sensitive 595.91.07",
    "bob@example.org 00:1a:2b:3c:4d:5e 123e4567-e89b-12d3-a456-426614174000",
]


@pytest.mark.parametrize("sample", SAMPLES)
def test_redaction_is_idempotent_and_the_guard_agrees(sample: str) -> None:
    once = R.text(sample)
    assert R.text(once) == once
    assert not R.leaves_something(once)
    assert R.leaves_something(sample) == (once != sample)


def test_the_guard_notices_text_that_was_not_redacted() -> None:
    assert R.leaves_something("password: hunter2")
    assert not R.leaves_something("password=[redacted]")


def test_obj_redacts_strings_in_nested_data_but_not_keys_or_other_types() -> None:
    data = {
        "10.0.0.5": {"ip": "10.0.0.5", "n": 5, "ok": True, "none": None},
        "list": ["alice", ["gpu-rig-07", 3.5]],
    }
    out = R.obj(data)
    assert "10.0.0.5" in out  # keys are fixed code, not host data
    assert out["10.0.0.5"] == {"ip": IP, "n": 5, "ok": True, "none": None}
    assert out["list"] == ["[user]", ["[host]", 3.5]]


def test_obj_does_not_change_the_original() -> None:
    data = {"a": ["10.0.0.5"]}
    R.obj(data)
    assert data == {"a": ["10.0.0.5"]}


def test_there_is_no_way_to_switch_redaction_off() -> None:
    assert not hasattr(R, "enabled") and not hasattr(R, "disable")
    import inspect

    assert "enabled" not in inspect.signature(Redactor).parameters


def test_local_identifiers_returns_lists_and_never_raises() -> None:
    hosts, users, ids = local_identifiers()
    assert all(isinstance(x, str) for x in (*hosts, *users, *ids))


# -- found by independent testing (F-03, F-11, F-12, F-23) -----------------------------------------


@pytest.mark.parametrize(
    ("hostname", "username"),
    [("host", "user"), ("ab", "bob"), ("redacted", "key"), ("ip", "mac")],
)
def test_names_that_match_our_own_placeholders_still_redact_idempotently(
    hostname: str, username: str
) -> None:
    """F-03: a host named `host` used to make `[host]` become `[[host]]`, so the guard
    refused every report."""
    r = Redactor([hostname], [username], [])
    sample = f"{hostname} {username} /home/{username}/x [redacted-ip] 10.0.0.5 {hostname}.{username}"
    once = r.text(sample)
    assert r.text(once) == once  # idempotent
    assert not r.leaves_something(once)
    assert "10.0.0.5" not in once and f"/home/{username}/" not in once


@pytest.mark.parametrize(
    "sample",
    ["vm_10.0.0.5_data", "node10.0.0.5x", "pod-192.168.1.20-b", "a_8.8.4.4.5_b"],
)
def test_an_address_is_redacted_even_when_glued_to_letters_or_underscores(
    sample: str,
) -> None:
    out = R.text(sample)
    digits = [p for p in ("10.0.0.5", "192.168.1.20") if p in sample]
    assert all(d not in out for d in digits)


@pytest.mark.parametrize(
    "sample", ["user_alice", "alice_data", "my-alice-box", "xalice.y".replace("x", "_")]
)
def test_a_known_user_name_is_redacted_when_joined_by_a_separator(sample: str) -> None:
    assert "alice" not in R.text(sample).lower()


def test_a_name_inside_a_longer_word_is_left_alone() -> None:
    assert R.text("malicenter") == "malicenter"  # `alice` is not a separate word here


def test_an_ipv4_mapped_ipv6_address_is_fully_redacted() -> None:
    out = R.text("peer ::ffff:10.0.0.5 and ::ffff:192.168.1.1")
    assert "10.0.0.5" not in out and "192.168.1.1" not in out and "ffff" not in out


def test_a_secret_later_in_the_same_token_is_still_found() -> None:
    assert "hunter2" not in R.text("a=password=hunter2")
    assert "hunter2" not in R.text("name=ok;password=hunter2")


def test_values_of_secret_named_keys_are_redacted_and_other_keys_are_kept() -> None:
    out = R.text("API_KEY=abc123 mode=fast")
    assert "abc123" not in out and "mode=fast" in out


@pytest.mark.parametrize(
    "build",
    [
        lambda n: "a" * n,
        lambda n: "a." * (n // 2),
        lambda n: "token" * (n // 5),
        lambda n: "x@" * (n // 2),
        lambda n: "a-b" * (n // 3),
        lambda n: "api_key=" * (n // 8),
        lambda n: ' key="' * (n // 6),
    ],
)
def test_redaction_time_grows_linearly_not_quadratically(build) -> None:  # type: ignore[no-untyped-def]
    """F-12: 100,000 characters used to take over 20 seconds."""
    import time

    start = time.monotonic()
    R.text(build(100_000))
    assert time.monotonic() - start < 3.0
