from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox import transfer as transfer_module
from talonbox.transfer import parse_rsync_args, parse_scp_args
from tests.helpers import build_service_stack


def build_rsync_args(args: list[str]) -> list[str]:
    _, transfer_service, _ = build_service_stack()
    return transfer_service._build_transfer_command_args(
        parse_rsync_args(args), transfer_service.running_vm
    )


def build_scp_args(args: list[str]) -> list[str]:
    _, transfer_service, _ = build_service_stack()
    return transfer_service._build_transfer_command_args(
        parse_scp_args(args), transfer_service.running_vm
    )


def test_transfer_service_rsync_rewrites_vm_destination() -> None:
    args = build_rsync_args(
        ["-av", "./repo/", "talon-test:/Users/admin/.talon/user/repo/"]
    )

    assert args == [
        "-av",
        "./repo/",
        "admin@192.168.64.10:/Users/admin/.talon/user/repo/",
    ]


def test_transfer_service_scp_download_rewrites_vm_source() -> None:
    args = build_scp_args(["talon-test:/tmp/out.png", "/tmp/out.png"])

    assert args == [
        "admin@192.168.64.10:/tmp/out.png",
        str(Path("/tmp/out.png").resolve(strict=False)),
    ]


def test_transfer_service_extracts_vm_name_from_operands() -> None:
    assert (
        transfer_module.parse_rsync_args(
            ["-av", "./repo/", "experiment:/tmp/repo/"]
        ).vm_name
        == "experiment"
    )


def test_transfer_service_rejects_mixed_vm_names() -> None:
    with pytest.raises(click.ClickException, match="same VM"):
        transfer_module.parse_rsync_args(
            ["-av", "one:/tmp/a", "two:/tmp/b", "/tmp/out/"]
        )


def test_transfer_service_rejects_transport_override() -> None:
    with pytest.raises(click.ClickException, match="Option not allowed"):
        parse_rsync_args(["-e", "ssh", "./repo/", "talon-test:/tmp/repo/"])


def test_transfer_service_rejects_unknown_rsync_option() -> None:
    with pytest.raises(click.ClickException, match="--server"):
        parse_rsync_args(["--server", "./repo/", "talon-test:/tmp/repo/"])


def test_transfer_service_rejects_unknown_scp_option() -> None:
    with pytest.raises(click.ClickException, match="-Y"):
        parse_scp_args(["-Y", "./settings.talon", "talon-test:/tmp/settings.talon"])


def test_transfer_service_rejects_scp_transport_override() -> None:
    with pytest.raises(click.ClickException, match="Option not allowed"):
        parse_scp_args(
            [
                "-o",
                "ProxyCommand=sh",
                "./settings.talon",
                "talon-test:/tmp/settings.talon",
            ]
        )


def test_transfer_service_allows_rsync_host_write_flag_inside_sandbox() -> None:
    args = build_rsync_args(
        ["--log-file=/tmp/talonbox-rsync.log", "./repo/", "talon-test:/tmp/repo/"]
    )

    assert args == [
        "--log-file=/tmp/talonbox-rsync.log",
        "./repo/",
        "admin@192.168.64.10:/tmp/repo/",
    ]


def test_transfer_service_allows_rsync_value_option() -> None:
    args = build_rsync_args(["--exclude", "*.pyc", "./repo/", "talon-test:/tmp/repo/"])

    assert args == [
        "--exclude",
        "*.pyc",
        "./repo/",
        "admin@192.168.64.10:/tmp/repo/",
    ]


def test_transfer_service_allows_scp_value_option() -> None:
    args = build_scp_args(
        ["-P", "22", "./settings.talon", "talon-test:/tmp/settings.talon"]
    )

    assert args == [
        "-P",
        "22",
        "./settings.talon",
        "admin@192.168.64.10:/tmp/settings.talon",
    ]


def test_transfer_service_rejects_vm_to_vm() -> None:
    with pytest.raises(click.ClickException, match="VM-to-VM"):
        parse_scp_args(["talon-test:/tmp/a", "talon-test:/tmp/b"])


def test_transfer_service_rejects_local_to_local() -> None:
    with pytest.raises(click.ClickException, match="Transfer requires one VM operand"):
        parse_rsync_args(["-av", "./repo/", "/Users/admin/.talon/user/repo/"])


def test_transfer_service_rejects_symlink_escape_from_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, _ = build_service_stack()
    escape_root = tmp_path.resolve()
    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir()
    (escape_root / "link").symlink_to(outside_dir, target_is_directory=True)

    monkeypatch.setattr(transfer_service, "_host_output_root", lambda: escape_root)

    with pytest.raises(
        click.ClickException, match="Symlinks that escape /tmp are not allowed."
    ):
        parsed_args = parse_rsync_args(
            ["-av", "talon-test:/tmp/out.txt", str(escape_root / "link" / "out.txt")]
        )
        transfer_service._build_transfer_command_args(
            parsed_args, transfer_service.running_vm
        )


def test_transfer_service_rsync_uses_fixed_vm_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []
    _, transfer_service, _ = build_service_stack()
    monkeypatch.setattr(
        transfer_service,
        "_sandbox_command_prefix",
        lambda: ["sandbox-exec", "-p", "(profile)"],
    )

    def fake_run(
        cmd: list[str],
        *,
        check: bool,
        text: bool,
        capture_output: bool,
        stdin: object,
    ) -> subprocess.CompletedProcess[str]:
        del check, text, capture_output, stdin
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)

    returncode = transfer_service.rsync(
        parse_rsync_args(["-av", "src/", "talon-test:/tmp/dest"])
    )

    assert returncode == 0
    assert recorded == [
        [
            "sandbox-exec",
            "-p",
            "(profile)",
            "rsync",
            "-e",
            "sshpass -p admin ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=no -o NumberOfPasswordPrompts=1 -o PasswordAuthentication=yes -o KbdInteractiveAuthentication=no -o PreferredAuthentications=password -o PubkeyAuthentication=no",
            "-av",
            "src/",
            "admin@192.168.64.10:/tmp/dest",
        ]
    ]


def test_transfer_service_rsync_retries_transient_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    _, transfer_service, _ = build_service_stack()
    monkeypatch.setattr(
        transfer_service,
        "_sandbox_command_prefix",
        lambda: ["sandbox-exec", "-p", "(profile)"],
    )
    monkeypatch.setattr("talonbox.transfer.time.sleep", lambda seconds: None)

    def fake_run(
        cmd: list[str],
        *,
        check: bool,
        text: bool,
        capture_output: bool,
        stdin: object,
    ) -> subprocess.CompletedProcess[str]:
        del check, text, capture_output, stdin
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(
                cmd,
                255,
                "",
                "ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such file or directory\n"
                "admin@192.168.64.10: Permission denied (publickey,password,keyboard-interactive).",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)

    returncode = transfer_service.rsync(
        parse_rsync_args(["-av", "src/", "talon-test:/tmp/dest"])
    )

    assert returncode == 0
    assert attempts["count"] == 2


def test_transfer_service_scp_uses_fixed_vm_ssh_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []
    _, transfer_service, _ = build_service_stack()
    monkeypatch.setattr(
        transfer_service,
        "_sandbox_command_prefix",
        lambda: ["sandbox-exec", "-p", "(profile)"],
    )

    def fake_run(
        cmd: list[str],
        *,
        check: bool,
        text: bool,
        capture_output: bool,
        stdin: object,
    ) -> subprocess.CompletedProcess[str]:
        del check, text, capture_output, stdin
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)

    returncode = transfer_service.scp(
        parse_scp_args(["./settings.talon", "talon-test:/tmp/settings.talon"])
    )

    assert returncode == 0
    assert recorded == [
        [
            "sandbox-exec",
            "-p",
            "(profile)",
            "sshpass",
            "-p",
            "admin",
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "BatchMode=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "./settings.talon",
            "admin@192.168.64.10:/tmp/settings.talon",
        ]
    ]


def test_transfer_service_sandbox_profile_allows_tmp_and_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, transfer_service, _ = build_service_stack()

    monkeypatch.setattr(transfer_module, "HOST_OUTPUT_ROOT", Path("/tmp"))
    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: Path("/private/tmp")
    )

    profile = transfer_service._sandbox_profile()

    assert "(deny file-write*)" in profile
    assert '(allow file-write* (subpath "/private/tmp"))' in profile
    assert '(allow file-write* (subpath "/tmp"))' in profile
    assert '(allow file-write* (subpath "/dev"))' in profile
