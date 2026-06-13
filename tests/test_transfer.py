from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

from talonbox import transfer as transfer_module
from tests.helpers import build_service_stack


def test_transfer_service_rsync_rewrites_vm_destination() -> None:
    _, transfer_service, _ = build_service_stack()

    args = transfer_service.prepare_rsync_args(
        ["-av", "./repo/", "talon-test:/Users/lume/.talon/user/repo/"]
    )

    assert args == [
        "-av",
        "./repo/",
        "lume@192.168.64.10:/Users/lume/.talon/user/repo/",
    ]


def test_transfer_service_scp_download_rewrites_vm_source() -> None:
    _, transfer_service, _ = build_service_stack()

    args = transfer_service.prepare_scp_args(
        ["talon-test:/tmp/out.png", "/tmp/out.png"]
    )

    assert args == [
        "lume@192.168.64.10:/tmp/out.png",
        str(Path("/tmp/out.png").resolve(strict=False)),
    ]


def test_transfer_service_extracts_vm_name_from_operands() -> None:
    assert (
        transfer_module.TransferService.extract_rsync_vm_name(
            ["-av", "./repo/", "experiment:/tmp/repo/"]
        )
        == "experiment"
    )


def test_transfer_service_rejects_guest_prefix() -> None:
    _, transfer_service, _ = build_service_stack()

    with pytest.raises(click.ClickException, match="guest: paths have been replaced"):
        transfer_service.prepare_rsync_args(["-av", "./repo/", "guest:/tmp/repo/"])


def test_transfer_service_rejects_mixed_vm_names() -> None:
    with pytest.raises(click.ClickException, match="same VM"):
        transfer_module.TransferService.extract_rsync_vm_name(
            ["-av", "one:/tmp/a", "two:/tmp/b", "/tmp/out/"]
        )


def test_transfer_service_rejects_transport_override() -> None:
    _, transfer_service, _ = build_service_stack()

    with pytest.raises(click.ClickException, match="Option not allowed"):
        transfer_service.prepare_rsync_args(
            ["-e", "ssh", "./repo/", "talon-test:/tmp/repo/"]
        )


def test_transfer_service_allows_rsync_host_write_flag_inside_sandbox() -> None:
    _, transfer_service, _ = build_service_stack()

    args = transfer_service.prepare_rsync_args(
        ["--log-file=/tmp/talonbox-rsync.log", "./repo/", "talon-test:/tmp/repo/"]
    )

    assert args == [
        "--log-file=/tmp/talonbox-rsync.log",
        "./repo/",
        "lume@192.168.64.10:/tmp/repo/",
    ]


def test_transfer_service_rejects_vm_to_vm() -> None:
    _, transfer_service, _ = build_service_stack()

    with pytest.raises(click.ClickException, match="VM-to-VM"):
        transfer_service.prepare_scp_args(["talon-test:/tmp/a", "talon-test:/tmp/b"])


def test_transfer_service_rejects_local_to_local() -> None:
    _, transfer_service, _ = build_service_stack()

    with pytest.raises(click.ClickException, match="Transfer requires one VM operand"):
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
            ["-av", "talon-test:/tmp/out.txt", str(escape_root / "link" / "out.txt")]
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

    returncode = transfer_service.rsync(["-av", "src/", "talon-test:/tmp/dest"])

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

    returncode = transfer_service.scp(
        ["./settings.talon", "talon-test:/tmp/settings.talon"]
    )

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
