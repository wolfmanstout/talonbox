from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

import click

from . import lume

TALON_BINARY = "/Applications/Talon.app/Contents/MacOS/Talon"
TALON_LOG = "$HOME/.talon/talon.log"
START_TIMEOUT_SECONDS = 180.0
SSH_TIMEOUT_SECONDS = 60.0
TALON_TIMEOUT_SECONDS = 30.0
TALON_REPL_TIMEOUT_SECONDS = 30.0
TALON_POST_RESTART_SETTLE_SECONDS = 3.0
TRANSIENT_RETRY_DELAY_SECONDS = 1.0
TRANSIENT_RETRY_ATTEMPTS = 2
DEFAULT_GOLDEN_VM = "talonbox-golden"


class TransportError(RuntimeError):
    pass


class RemoteCommandError(TransportError):
    pass


def _process_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or "").strip() or (result.stdout or "").strip()


class RunningVm:
    SSH_USERNAME = "lume"
    SSH_PASSWORD = "lume"
    SSH_OPTIONS = [
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
    ]

    def __init__(
        self,
        *,
        name: str,
        ip_address: str,
        debug: bool,
        vnc_url: str | None = None,
    ) -> None:
        self.name = name
        self.ip_address = ip_address
        self.debug = debug
        self.vnc_url = vnc_url

    def to_vm_info(self) -> lume.VmInfo:
        return lume.VmInfo(
            name=self.name,
            status="running",
            ip_address=self.ip_address,
            vnc_url=self.vnc_url,
        )

    def run_shell(
        self,
        command: str | list[str],
        *,
        timeout: float | None = None,
        poll: bool = False,
        stream: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        remote_command = command if isinstance(command, str) else shlex.join(command)
        result = self._run_transport_command(
            [*self._ssh_command_prefix(), f"sh -lc {shlex.quote(remote_command)}"],
            timeout=timeout,
            poll=poll,
            stream=stream,
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() if result.stderr else ""
            if not message and result.stdout:
                message = result.stdout.strip()
            raise RemoteCommandError(
                message or f"Remote command failed: {remote_command}"
            )
        return result

    def run_repl(
        self,
        payload: str,
        *,
        stream_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run_transport_command(
            [*self._ssh_command_prefix(), 'sh -lc "$HOME/.talon/bin/repl"'],
            input_text=payload,
        )
        if stream_output or result.returncode != 0:
            if result.stdout:
                sys.stdout.write(result.stdout)
            if result.stderr:
                sys.stderr.write(result.stderr)
        return result

    def wait_for_talon_repl(
        self,
        *,
        timeout: float = TALON_REPL_TIMEOUT_SECONDS,
    ) -> None:
        self.run_shell(
            'test -S "$HOME/.talon/.sys/repl.sock"',
            timeout=timeout,
            poll=True,
        )

    def probe_ssh(self, *, timeout: float = SSH_TIMEOUT_SECONDS) -> None:
        self.run_shell(
            "true",
            timeout=timeout,
            poll=True,
        )

    def download(self, remote_path: str, local_path: Path) -> None:
        result = self._run_transport_command(
            [
                *self.scp_command_prefix(),
                self.ssh_remote_path(remote_path),
                str(local_path),
            ],
        )
        if result.returncode != 0:
            message = _process_output(result)
            if not message:
                message = "failed to download file from guest"
            raise TransportError(message)

    def ssh_remote_path(self, guest_path: str) -> str:
        return f"{self.SSH_USERNAME}@{self.ip_address}:{guest_path}"

    def restart_talon(
        self,
        *,
        wipe_user_dir: bool,
        clean_logs: bool,
    ) -> None:
        self.run_shell("pkill -x Talon >/dev/null 2>&1 || true")
        if clean_logs:
            self.run_shell(
                f'mkdir -p "$HOME/.talon" && : > {TALON_LOG} && : > /tmp/talonbox-talon.log'
            )
        self.run_shell('mkdir -p "$HOME/.talon/user"')
        if wipe_user_dir:
            self.run_shell(
                'find "$HOME/.talon/user" -mindepth 1 -maxdepth 1 -exec rm -rf {} +'
            )
        script_path = "/tmp/talonbox-launch.command"
        script_body = f"#!/bin/sh\nexec arch -x86_64 {TALON_BINARY} >/tmp/talonbox-talon.log 2>&1\n"
        self.run_shell(
            f"printf %s {shlex.quote(script_body)} > {shlex.quote(script_path)} && "
            f"chmod +x {shlex.quote(script_path)} && "
            f"open -a Terminal {shlex.quote(script_path)}"
        )
        self.run_shell(
            "pgrep -x Talon >/dev/null",
            timeout=TALON_TIMEOUT_SECONDS,
            poll=True,
        )
        self.wait_for_talon_repl(timeout=TALON_REPL_TIMEOUT_SECONDS)
        time.sleep(TALON_POST_RESTART_SETTLE_SECONDS)

    def _ssh_command_prefix(self) -> list[str]:
        return [
            "sshpass",
            "-p",
            self.SSH_PASSWORD,
            "ssh",
            *self.SSH_OPTIONS,
            f"{self.SSH_USERNAME}@{self.ip_address}",
        ]

    def scp_command_prefix(self) -> list[str]:
        return ["sshpass", "-p", self.SSH_PASSWORD, "scp", *self.SSH_OPTIONS]

    def ssh_command_for_rsync(self) -> str:
        return shlex.join(
            ["sshpass", "-p", self.SSH_PASSWORD, "ssh", *self.SSH_OPTIONS]
        )

    def _run_transport_command(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        poll: bool = False,
        stream: bool = False,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.debug:
            click.echo(f"+ {shlex.join(cmd)}", err=True)

        deadline = time.monotonic() + timeout if poll and timeout is not None else None
        attempts = 0
        while True:
            result = subprocess.run(
                cmd,
                check=False,
                text=True,
                capture_output=not stream,
                timeout=None if poll else timeout,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                input=input_text,
            )
            if result.returncode == 0 or not poll:
                if result.returncode == 0:
                    return result
                if attempts < TRANSIENT_RETRY_ATTEMPTS:
                    message = _process_output(result).lower()
                    if any(
                        needle in message
                        for needle in (
                            "ssh_askpass",
                            "permission denied (publickey,password,keyboard-interactive)",
                            "connection reset by peer",
                            "connection refused",
                            "connection closed by remote host",
                            "operation timed out",
                            "no route to host",
                            "kex_exchange_identification",
                            "broken pipe",
                        )
                    ):
                        attempts += 1
                        time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                        continue
                return result
            if deadline is not None and time.monotonic() >= deadline:
                return result
            time.sleep(2.0)


class VmController:
    def __init__(
        self, vm: str, debug: bool, golden_vm: str = DEFAULT_GOLDEN_VM
    ) -> None:
        self.vm = vm
        self.debug = debug
        self.golden_vm = golden_vm

    def debug_log(self, message: str) -> None:
        if self.debug:
            click.echo(message, err=True)

    def get_vm(self) -> lume.VmInfo:
        info = self._get_vm_info(self.vm)
        if info is None:
            raise click.ClickException(f"VM not found: {self.vm}")
        return info

    def _get_vm_info(self, name: str) -> lume.VmInfo | None:
        try:
            return lume.get_vm_info(name, debug=self.debug)
        except lume.LumeError as error:
            raise click.ClickException(str(error)) from None

    def get_running_vm(self) -> RunningVm:
        info = self.get_vm()
        return self._running_vm_from_info(info)

    def format_vm_info(self, info: lume.VmInfo) -> list[str]:
        lines = [f"status: {info.status}"]
        if info.status == "running" and info.ip_address:
            lines.extend(
                [
                    f"ip: {info.ip_address}",
                    f"username: {RunningVm.SSH_USERNAME}",
                    f"password: {RunningVm.SSH_PASSWORD}",
                ]
            )
            if info.vnc_url:
                lines.append(f"vnc: {info.vnc_url}")
        return lines

    def start(self, *, resume: bool = False) -> RunningVm:
        if self.vm == self.golden_vm:
            raise click.ClickException(
                f"Target VM and golden VM must be different: {self.vm}"
            )

        info = self._prepare_start_vm(resume=resume)
        if info.status != "stopped":
            raise click.ClickException(f"VM is not stopped: {self.vm} ({info.status})")

        launch = None
        try:
            launch = lume.spawn_vm(self.vm, debug=self.debug)
            ready_info = lume.wait_for_running_vm(
                self.vm,
                timeout=START_TIMEOUT_SECONDS,
                debug=self.debug,
                launch=launch,
            )
            running_vm = self._running_vm_from_info(ready_info)
            running_vm.probe_ssh(timeout=SSH_TIMEOUT_SECONDS)
            running_vm.restart_talon(wipe_user_dir=True, clean_logs=True)
        except (lume.LumeError, RemoteCommandError, TransportError) as error:
            if launch is not None and launch.process.poll() is None:
                self._cleanup_failed_start()
            raise click.ClickException(str(error)) from None

        lume.cleanup_launch_log(launch.log_path)
        return running_vm

    def _prepare_start_vm(self, *, resume: bool) -> lume.VmInfo:
        if resume:
            return self.get_vm()

        golden_info = self._get_vm_info(self.golden_vm)
        if golden_info is None:
            raise click.ClickException(f"Golden VM not found: {self.golden_vm}")

        target_info = self._get_vm_info(self.vm)
        if target_info is not None:
            if target_info.status == "running":
                raise click.ClickException(f"VM is already running: {self.vm}")
            if target_info.status != "stopped":
                raise click.ClickException(
                    f"VM is not stopped: {self.vm} ({target_info.status})"
                )
            try:
                lume.delete_vm(self.vm, debug=self.debug)
            except lume.LumeError as error:
                raise click.ClickException(str(error)) from None

        try:
            lume.clone_vm(self.golden_vm, self.vm, debug=self.debug)
        except lume.LumeError as error:
            raise click.ClickException(str(error)) from None

        info = self._get_vm_info(self.vm)
        if info is None:
            raise click.ClickException(f"VM not found after clone: {self.vm}")
        return info

    def restart_talon(
        self,
        *,
        wipe_user_dir: bool,
        clean_logs: bool,
    ) -> None:
        self.get_running_vm().restart_talon(
            wipe_user_dir=wipe_user_dir,
            clean_logs=clean_logs,
        )

    def stop(self) -> None:
        info = self.get_vm()
        if info.status == "stopped":
            return

        try:
            lume.stop_vm(self.vm, debug=self.debug)
            lume.wait_for_status(self.vm, "stopped", timeout=60.0, debug=self.debug)
        except lume.LumeError as error:
            self.debug_log(f"graceful stop failed: {error}")
            try:
                lume.force_stop_vm(self.vm, debug=self.debug)
                lume.wait_for_status(self.vm, "stopped", timeout=20.0, debug=self.debug)
            except lume.LumeError as force_error:
                raise click.ClickException(str(force_error)) from None

    def _cleanup_failed_start(self) -> None:
        self.debug_log("start failed; stopping VM")
        try:
            lume.stop_vm(self.vm, debug=self.debug)
            lume.wait_for_status(self.vm, "stopped", timeout=30.0, debug=self.debug)
        except lume.LumeError as error:
            self.debug_log(f"cleanup stop failed: {error}")

    def _running_vm_from_info(self, info: lume.VmInfo) -> RunningVm:
        if info.status != "running" or not info.ip_address:
            raise click.ClickException(f"VM is not running: {self.vm}")
        return RunningVm(
            name=info.name,
            ip_address=info.ip_address,
            debug=self.debug,
            vnc_url=info.vnc_url,
        )
