from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import click

from .vm import RunningVm

HOST_OUTPUT_ROOT = Path("/tmp")
GUEST_PREFIX = "guest:"
RSYNC_VALUE_OPTIONS = {
    "-B",
    "-f",
    "-M",
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
RSYNC_REJECTED_OPTIONS = {
    "-T",
    "-e",
    "--backup-dir",
    "--log-file",
    "--only-write-batch",
    "--partial-dir",
    "--rsync-path",
    "--rsh",
    "--temp-dir",
    "--write-batch",
}
SCP_VALUE_OPTIONS = {"-c", "-D", "-i", "-l", "-o", "-P", "-S", "-X"}
SCP_REJECTED_OPTIONS = {"-F", "-J", "-o", "-S"}


@dataclass(frozen=True, slots=True)
class TransferOperand:
    raw: str
    kind: str
    path: str


class TransferService:
    def __init__(self, running_vm: RunningVm) -> None:
        self.running_vm = running_vm

    def prepare_rsync_args(self, args: Sequence[str]) -> list[str]:
        return self._build_transfer_command_args(
            args,
            self.running_vm,
            value_options=RSYNC_VALUE_OPTIONS,
            rejected_options=RSYNC_REJECTED_OPTIONS,
        )

    def prepare_scp_args(self, args: Sequence[str]) -> list[str]:
        return self._build_transfer_command_args(
            args,
            self.running_vm,
            value_options=SCP_VALUE_OPTIONS,
            rejected_options=SCP_REJECTED_OPTIONS,
        )

    def rsync(self, args: Sequence[str]) -> int:
        return self._run_transfer(
            [
                "rsync",
                "-e",
                self.running_vm.ssh_command_for_rsync(),
                *self._build_transfer_command_args(
                    args,
                    self.running_vm,
                    value_options=RSYNC_VALUE_OPTIONS,
                    rejected_options=RSYNC_REJECTED_OPTIONS,
                ),
            ],
        )

    def scp(self, args: Sequence[str]) -> int:
        return self._run_transfer(
            [
                *self.running_vm.scp_command_prefix(),
                *self._build_transfer_command_args(
                    args,
                    self.running_vm,
                    value_options=SCP_VALUE_OPTIONS,
                    rejected_options=SCP_REJECTED_OPTIONS,
                ),
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
        args: Sequence[str],
        running_vm: RunningVm,
        *,
        value_options: set[str],
        rejected_options: set[str],
    ) -> list[str]:
        passthrough, positionals = self._split_transfer_options_and_operands(
            args,
            value_options=value_options,
            rejected_options=rejected_options,
        )
        if len(positionals) < 2:
            raise click.ClickException(
                "Transfer requires at least one source and one destination"
            )

        sources = [self._classify_transfer_operand(arg) for arg in positionals[:-1]]
        destination = self._classify_transfer_operand(positionals[-1])

        source_kinds = {source.kind for source in sources}
        if len(source_kinds) != 1:
            raise click.ClickException("Mixed local and guest sources are not allowed")
        source_kind = next(iter(source_kinds))
        if source_kind == destination.kind:
            if source_kind == "local":
                raise click.ClickException(
                    "Local-to-local transfers are not allowed; use guest: paths for the VM"
                )
            raise click.ClickException("Guest-to-guest transfers are not allowed")
        if destination.kind == "local":
            destination = TransferOperand(
                raw=destination.raw,
                kind=destination.kind,
                path=str(self.normalize_local_output_path(destination.path)),
            )

        rewritten = [
            self._rewrite_transfer_operand(running_vm, operand)
            for operand in [*sources, destination]
        ]
        return [*passthrough, *rewritten]

    def _split_transfer_options_and_operands(
        self,
        args: Sequence[str],
        *,
        value_options: set[str],
        rejected_options: set[str],
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
                if option in rejected_options:
                    raise click.ClickException(
                        f"Option not allowed for VM-only transfer safety: {option}"
                    )
                passthrough.append(arg)
                index += 1
                if has_value or option not in value_options:
                    continue
                if index >= len(args):
                    raise click.ClickException(f"Option requires a value: {option}")
                passthrough.append(args[index])
                index += 1
                continue

            short_option = arg[:2]
            if short_option in rejected_options:
                raise click.ClickException(
                    f"Option not allowed for VM-only transfer safety: {short_option}"
                )
            passthrough.append(arg)
            index += 1
            if short_option not in value_options or len(arg) > 2:
                continue
            if index >= len(args):
                raise click.ClickException(f"Option requires a value: {short_option}")
            passthrough.append(args[index])
            index += 1

        return passthrough, positionals

    def _classify_transfer_operand(self, raw: str) -> TransferOperand:
        if raw.startswith(GUEST_PREFIX):
            path = raw[len(GUEST_PREFIX) :]
            if not path:
                raise click.ClickException("Guest path must not be empty: guest:/path")
            if not path.startswith("/"):
                raise click.ClickException(f"Guest path must be absolute: {raw}")
            return TransferOperand(raw=raw, kind="guest", path=path)
        if raw.startswith("rsync://"):
            raise click.ClickException(f"Only guest: remote paths are allowed: {raw}")
        if ":" in raw:
            raise click.ClickException(f"Only guest: remote paths are allowed: {raw}")
        return TransferOperand(raw=raw, kind="local", path=raw)

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

    def _run_transfer(self, cmd: list[str]) -> int:
        if self.running_vm.debug:
            click.echo(f"+ {shlex.join(cmd)}", err=True)
        result = subprocess.run(cmd, check=False)
        return result.returncode
