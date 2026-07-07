from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import click

from .names import validate_public_vm_name
from .vm import (
    TRANSIENT_RETRY_ATTEMPTS,
    TRANSIENT_RETRY_DELAY_SECONDS,
    RunningVm,
    is_transient_transport_error,
)

HOST_OUTPUT_ROOT = Path("/tmp")
DEVICE_ROOT = Path("/dev")


@dataclass(frozen=True, slots=True)
class TransferOptionPolicy:
    short_flags: set[str]
    long_flags: set[str]
    value_options: set[str]


RSYNC_SHORT_FLAG_OPTIONS = {
    "-A",
    "-C",
    "-D",
    "-E",
    "-H",
    "-I",
    "-L",
    "-O",
    "-P",
    "-R",
    "-S",
    "-W",
    "-X",
    "-a",
    "-c",
    "-d",
    "-g",
    "-h",
    "-l",
    "-n",
    "-o",
    "-p",
    "-q",
    "-r",
    "-t",
    "-u",
    "-v",
    "-x",
    "-z",
}
RSYNC_LONG_FLAG_OPTIONS = {
    "--acls",
    "--archive",
    "--checksum",
    "--compress",
    "--copy-links",
    "--cvs-exclude",
    "--delete",
    "--delete-after",
    "--delete-before",
    "--delete-delay",
    "--delete-during",
    "--devices",
    "--dirs",
    "--dry-run",
    "--executability",
    "--existing",
    "--group",
    "--hard-links",
    "--human-readable",
    "--ignore-existing",
    "--ignore-times",
    "--inplace",
    "--itemize-changes",
    "--links",
    "--mkpath",
    "--omit-dir-times",
    "--one-file-system",
    "--owner",
    "--partial",
    "--perms",
    "--progress",
    "--prune-empty-dirs",
    "--quiet",
    "--recursive",
    "--relative",
    "--sparse",
    "--specials",
    "--stats",
    "--times",
    "--update",
    "--verbose",
    "--whole-file",
    "--xattrs",
}
RSYNC_OPTIONS_WITH_VALUES = {
    "-B",
    "-f",
    "-T",
    "--backup-dir",
    "--block-size",
    "--bwlimit",
    "--chmod",
    "--compare-dest",
    "--compress-choice",
    "--copy-dest",
    "--exclude",
    "--exclude-from",
    "--files-from",
    "--filter",
    "--iconv",
    "--include",
    "--include-from",
    "--link-dest",
    "--log-file",
    "--log-file-format",
    "--max-size",
    "--min-size",
    "--out-format",
    "--partial-dir",
    "--password-file",
    "--skip-compress",
    "--suffix",
    "--temp-dir",
}
SCP_SHORT_FLAG_OPTIONS = {
    "-1",
    "-2",
    "-3",
    "-4",
    "-6",
    "-A",
    "-B",
    "-C",
    "-O",
    "-p",
    "-q",
    "-r",
    "-T",
    "-v",
}
SCP_LONG_FLAG_OPTIONS: set[str] = set()
SCP_OPTIONS_WITH_VALUES = {"-c", "-i", "-l", "-P", "-X"}
RSYNC_OPTION_POLICY = TransferOptionPolicy(
    short_flags=RSYNC_SHORT_FLAG_OPTIONS,
    long_flags=RSYNC_LONG_FLAG_OPTIONS,
    value_options=RSYNC_OPTIONS_WITH_VALUES,
)
SCP_OPTION_POLICY = TransferOptionPolicy(
    short_flags=SCP_SHORT_FLAG_OPTIONS,
    long_flags=SCP_LONG_FLAG_OPTIONS,
    value_options=SCP_OPTIONS_WITH_VALUES,
)


@dataclass(frozen=True, slots=True)
class TransferOperand:
    raw: str
    kind: str
    path: str
    vm: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedTransferArgs:
    passthrough: tuple[str, ...]
    operands: tuple[TransferOperand, ...]
    vm_name: str


def parse_rsync_args(args: Sequence[str]) -> ParsedTransferArgs:
    return _parse_transfer_args(args, option_policy=RSYNC_OPTION_POLICY)


def parse_scp_args(args: Sequence[str]) -> ParsedTransferArgs:
    return _parse_transfer_args(args, option_policy=SCP_OPTION_POLICY)


def _parse_transfer_args(
    args: Sequence[str],
    *,
    option_policy: TransferOptionPolicy,
) -> ParsedTransferArgs:
    passthrough, positionals = _split_transfer_options_and_operands(
        args, option_policy=option_policy
    )
    if len(positionals) < 2:
        raise click.ClickException(
            "Transfer requires at least one source and one destination"
        )

    operands = tuple(_classify_transfer_operand(arg) for arg in positionals)
    vm_names = {operand.vm for operand in operands if operand.kind == "remote"}
    if not vm_names:
        raise click.ClickException(
            "Transfer requires one VM operand written as NAME:/absolute/path"
        )
    if len(vm_names) != 1:
        raise click.ClickException("All VM operands must name the same VM")
    vm_name = next(iter(vm_names))
    assert vm_name is not None

    source_kinds = {source.kind for source in operands[:-1]}
    if len(source_kinds) != 1:
        raise click.ClickException("Mixed local and VM sources are not allowed")
    source_kind = next(iter(source_kinds))
    destination = operands[-1]
    if source_kind == destination.kind:
        if source_kind == "local":
            raise click.ClickException(
                "Local-to-local transfers are not allowed; use NAME:/path for the VM"
            )
        raise click.ClickException("VM-to-VM transfers are not allowed")

    return ParsedTransferArgs(
        passthrough=tuple(passthrough),
        operands=operands,
        vm_name=vm_name,
    )


def _split_transfer_options_and_operands(
    args: Sequence[str],
    *,
    option_policy: TransferOptionPolicy,
) -> tuple[list[str], list[str]]:
    passthrough: list[str] = []
    positionals: list[str] = []
    index = 0
    parsing_options = True

    while index < len(args):
        arg = args[index]
        if parsing_options and arg == "--":
            passthrough.append(arg)
            parsing_options = False
            index += 1
            continue
        if not parsing_options or not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            index += 1
            continue

        if arg.startswith("--"):
            option, has_value, _ = arg.partition("=")
            if (
                option not in option_policy.long_flags
                and option not in option_policy.value_options
            ):
                raise click.ClickException(
                    f"Option not allowed for VM-only transfer safety: {option}"
                )
            if has_value and option not in option_policy.value_options:
                raise click.ClickException(f"Option does not take a value: {option}")
            passthrough.append(arg)
            index += 1
            if has_value or option not in option_policy.value_options:
                continue
            if index >= len(args):
                raise click.ClickException(f"Option requires a value: {option}")
            passthrough.append(args[index])
            index += 1
            continue

        short_option = _validate_short_transfer_option(arg, option_policy=option_policy)
        passthrough.append(arg)
        index += 1
        if short_option not in option_policy.value_options or len(arg) > 2:
            continue
        if index >= len(args):
            raise click.ClickException(f"Option requires a value: {short_option}")
        passthrough.append(args[index])
        index += 1

    return passthrough, positionals


def _validate_short_transfer_option(
    arg: str,
    *,
    option_policy: TransferOptionPolicy,
) -> str:
    short_option = arg[:2]
    if short_option in option_policy.value_options:
        return short_option

    for character in arg[1:]:
        option = f"-{character}"
        if option in option_policy.value_options:
            raise click.ClickException(
                f"Option with a value must not be combined: {option}"
            )
        if option not in option_policy.short_flags:
            raise click.ClickException(
                f"Option not allowed for VM-only transfer safety: {option}"
            )
    return short_option


def _classify_transfer_operand(raw: str) -> TransferOperand:
    remote_name, separator, path = raw.partition(":")
    # Like scp and rsync, a '/' before the first ':' marks the operand as a
    # local path, so `./file:name` names a local file rather than a VM.
    if separator and "/" not in remote_name:
        if raw.startswith("rsync://"):
            raise click.ClickException(
                f"Only NAME:/path VM operands are allowed: {raw}"
            )
        remote_name = validate_public_vm_name(remote_name)
        if not path:
            raise click.ClickException(f"VM path must not be empty: {raw}")
        if not path.startswith("/"):
            raise click.ClickException(f"VM path must be absolute: {raw}")
        return TransferOperand(
            raw=raw,
            kind="remote",
            path=path,
            vm=remote_name,
        )
    return TransferOperand(raw=raw, kind="local", path=raw)


class TransferService:
    def __init__(self, running_vm: RunningVm) -> None:
        self.running_vm = running_vm

    def rsync(self, args: ParsedTransferArgs) -> int:
        return self._run_transfer(
            [
                *self._sandbox_command_prefix(),
                "rsync",
                "-e",
                self.running_vm.ssh_command_for_rsync(),
                *self._build_transfer_command_args(args, self.running_vm),
            ],
        )

    def scp(self, args: ParsedTransferArgs) -> int:
        return self._run_transfer(
            [
                *self._sandbox_command_prefix(),
                *self.running_vm.scp_command_prefix(),
                *self._build_transfer_command_args(args, self.running_vm),
            ]
        )

    def normalize_local_output_path(self, raw_path: str | Path) -> Path:
        destination = Path(raw_path).expanduser()
        if not destination.is_absolute():
            destination = Path.cwd() / destination

        try:
            resolved_destination = destination.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise click.ClickException(
                f"Unable to resolve local output path {raw_path!s}: {error}"
            ) from None

        host_output_root = self._host_output_root()
        if self._is_relative_to(resolved_destination, host_output_root):
            return resolved_destination

        raise click.ClickException(
            "Local output paths must stay under /tmp. "
            "Symlinks that escape /tmp are not allowed."
        )

    def _build_transfer_command_args(
        self,
        parsed_args: ParsedTransferArgs,
        running_vm: RunningVm,
    ) -> list[str]:
        if parsed_args.vm_name != running_vm.name:
            raise click.ClickException(
                f"Transfer operands name {parsed_args.vm_name!r}, but connected VM is {running_vm.name!r}"
            )

        sources = list(parsed_args.operands[:-1])
        destination = parsed_args.operands[-1]
        if destination.kind == "local":
            destination = TransferOperand(
                raw=destination.raw,
                kind=destination.kind,
                path=str(self.normalize_local_output_path(destination.path)),
                vm=destination.vm,
            )

        rewritten = [
            self._rewrite_transfer_operand(running_vm, operand)
            for operand in [*sources, destination]
        ]
        return [*parsed_args.passthrough, *rewritten]

    def _rewrite_transfer_operand(
        self, running_vm: RunningVm, operand: TransferOperand
    ) -> str:
        if operand.kind == "local":
            return operand.path
        return running_vm.ssh_remote_path(operand.path)

    def _host_output_root(self) -> Path:
        return HOST_OUTPUT_ROOT.resolve(strict=False)

    def _is_relative_to(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _sandbox_command_prefix(self) -> list[str]:
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec is None:
            raise click.ClickException(
                "sandbox-exec is required on macOS to enforce talonbox host write boundaries."
            )

        return [sandbox_exec, "-p", self._sandbox_profile()]

    def _sandbox_profile(self) -> str:
        host_output_root = self._host_output_root()
        writable_roots = {host_output_root}
        if host_output_root != HOST_OUTPUT_ROOT:
            writable_roots.add(HOST_OUTPUT_ROOT)

        write_rules = [
            f'(allow file-write* (subpath "{root}"))' for root in sorted(writable_roots)
        ]
        write_rules.append(f'(allow file-write* (subpath "{DEVICE_ROOT}"))')
        return " ".join(
            [
                "(version 1)",
                "(allow default)",
                "(deny file-write*)",
                *write_rules,
            ]
        )

    def _run_transfer(self, cmd: list[str]) -> int:
        if self.running_vm.debug:
            click.echo(f"+ {shlex.join(cmd)}", err=True)

        attempts = 0
        while True:
            result = subprocess.run(
                cmd,
                check=False,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                self._write_transfer_output(result)
                return result.returncode

            message = self._process_transfer_output(result)
            if attempts < TRANSIENT_RETRY_ATTEMPTS and is_transient_transport_error(
                message
            ):
                attempts += 1
                time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                continue

            self._write_transfer_output(result)
            if "operation not permitted" in message.lower():
                click.echo(
                    "HINT transfers run inside a macOS sandbox; extra host-side writes "
                    "outside /tmp fail with 'Operation not permitted'.",
                    err=True,
                )
            return result.returncode

    def _process_transfer_output(self, result: subprocess.CompletedProcess[str]) -> str:
        return "\n".join(part for part in (result.stderr, result.stdout) if part)

    def _write_transfer_output(self, result: subprocess.CompletedProcess[str]) -> None:
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
