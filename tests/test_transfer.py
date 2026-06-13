from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox import transfer as transfer_module
from tests.helpers import build_service_stack


def test_transfer_service_rsync_rewrites_guest_destination() -> None:
    _, transfer_service, _ = build_service_stack()

    args = transfer_service.prepare_rsync_args(
        ["-av", "./repo/", "guest:/Users/lume/.talon/user/repo/"]
    )

    assert args == [
        "-av",
        "./repo/",
        "lume@192.168.64.10:/Users/lume/.talon/user/repo/",
    ]


def test_transfer_service_scp_download_rewrites_guest_source() -> None:
    _, transfer_service, _ = build_service_stack()

    args = transfer_service.prepare_scp_args(["guest:/tmp/out.png", "/tmp/out.png"])

    assert args == [
        "lume@192.168.64.10:/tmp/out.png",
        str(Path("/tmp/out.png").resolve(strict=False)),
    ]


def test_transfer_service_rejects_transport_override() -> None:
    _, transfer_service, _ = build_service_stack()

    with pytest.raises(click.ClickException, match="Option not allowed"):
        transfer_service.prepare_rsync_args(
            ["-e", "ssh", "./repo/", "guest:/tmp/repo/"]
        )


def test_transfer_service_allows_rsync_host_write_flag_inside_sandbox() -> None:
    _, transfer_service, _ = build_service_stack()

    args = transfer_service.prepare_rsync_args(
        ["--log-file=/tmp/talonbox-rsync.log", "./repo/", "guest:/tmp/repo/"]
    )

    assert args == [
        "--log-file=/tmp/talonbox-rsync.log",
        "./repo/",
        "lume@192.168.64.10:/tmp/repo/",
    ]


def test_transfer_service_rejects_guest_to_guest() -> None:
    _, transfer_service, _ = build_service_stack()

    with pytest.raises(click.ClickException, match="Guest-to-guest"):
        transfer_service.prepare_scp_args(["guest:/tmp/a", "guest:/tmp/b"])


def test_transfer_service_rejects_local_to_local() -> None:
    _, transfer_service, _ = build_service_stack()

    with pytest.raises(
        click.ClickException, match="Local-to-local transfers are not allowed"
    ):
        transfer_service.prepare_rsync_args(
            ["-av", "./repo/", "/Users/lume/.talon/user/repo/"]
        )


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
        transfer_service.prepare_rsync_args(
            ["-av", "guest:/tmp/out.txt", str(escape_root / "link" / "out.txt")]
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
        cmd: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)

    returncode = transfer_service.rsync(["-av", "src/", "guest:/tmp/dest"])

    assert returncode == 0
    assert recorded == [
        [
            "sandbox-exec",
            "-p",
            "(profile)",
            "rsync",
            "-e",
            "sshpass -p lume ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=no -o NumberOfPasswordPrompts=1 -o PasswordAuthentication=yes -o KbdInteractiveAuthentication=no -o PreferredAuthentications=password -o PubkeyAuthentication=no",
            "-av",
            "src/",
            "lume@192.168.64.10:/tmp/dest",
        ]
    ]


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
        cmd: list[str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)

    returncode = transfer_service.scp(["./settings.talon", "guest:/tmp/settings.talon"])

    assert returncode == 0
    assert recorded == [
        [
            "sandbox-exec",
            "-p",
            "(profile)",
            "sshpass",
            "-p",
            "lume",
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
            "lume@192.168.64.10:/tmp/settings.talon",
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
