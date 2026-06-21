from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

import click

from . import lume
from .names import to_lume_vm_name, to_public_vm_name, validate_public_vm_name

TALON_APP = "/Applications/Talon.app"
TALON_LOG = "$HOME/.talon/talon.log"
START_TIMEOUT_SECONDS = 180.0
SSH_TIMEOUT_SECONDS = 60.0
TALON_TIMEOUT_SECONDS = 30.0
TALON_REPL_TIMEOUT_SECONDS = 30.0
TALON_REPL_COMMAND_TIMEOUT_SECONDS = 30.0
TALON_POST_RESTART_SETTLE_SECONDS = 3.0
TRANSIENT_RETRY_DELAY_SECONDS = 1.0
TRANSIENT_RETRY_ATTEMPTS = 2
CONCURRENCY_LIMIT_HINT = "macOS Virtualization commonly allows only 2 running VMs; stop another VM and retry."


class TransportError(RuntimeError):
    pass


class RemoteCommandError(TransportError):
    pass


def _process_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or "").strip() or (result.stdout or "").strip()


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


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
            timeout=TALON_REPL_COMMAND_TIMEOUT_SECONDS,
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

    def prevent_idle_lock(self) -> None:
        # idleTime prevents automatic screensaver activation; the password keys
        # keep other screensaver triggers from turning into a locked session.
        self.run_shell(
            "defaults -currentHost write com.apple.screensaver idleTime -int 0 && "
            "defaults write com.apple.screensaver askForPassword -int 0 && "
            "defaults write com.apple.screensaver askForPasswordDelay -int 0"
        )
        self.run_shell("killall cfprefsd >/dev/null 2>&1 || true")

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
        self._launch_talon(wipe_user_dir=wipe_user_dir, clean_logs=clean_logs)

    def ensure_talon_running(self) -> None:
        result = self.run_shell("pgrep -x Talon >/dev/null", check=False)
        if result.returncode == 0:
            self.wait_for_talon_repl(timeout=TALON_REPL_TIMEOUT_SECONDS)
            return
        self._launch_talon(wipe_user_dir=False, clean_logs=False)

    def _launch_talon(
        self,
        *,
        wipe_user_dir: bool,
        clean_logs: bool,
    ) -> None:
        if clean_logs:
            self.run_shell(
                f'mkdir -p "$HOME/.talon" && : > {TALON_LOG} && : > /tmp/talonbox-talon.log'
            )
        self.run_shell('mkdir -p "$HOME/.talon/user"')
        if wipe_user_dir:
            self.run_shell(
                'find "$HOME/.talon/user" -mindepth 1 -maxdepth 1 -exec rm -rf {} +'
            )
        launch_command = (
            f"open -a {shlex.quote(TALON_APP)} "
            "--stdout /tmp/talonbox-talon.log --stderr /tmp/talonbox-talon.log"
        )
        for attempt in range(TRANSIENT_RETRY_ATTEMPTS + 1):
            try:
                self.run_shell(launch_command)
                break
            except RemoteCommandError:
                if attempt == TRANSIENT_RETRY_ATTEMPTS:
                    raise
                time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
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
            try:
                result = subprocess.run(
                    cmd,
                    check=False,
                    text=True,
                    capture_output=not stream,
                    timeout=None if poll else timeout,
                    stdin=None if input_text is not None else subprocess.DEVNULL,
                    input=input_text,
                )
            except subprocess.TimeoutExpired as error:
                return subprocess.CompletedProcess(
                    cmd,
                    124,
                    _timeout_output(error.stdout),
                    _timeout_output(error.stderr)
                    or f"Command timed out after {timeout:.0f} seconds",
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
    def __init__(self, vm: str, debug: bool) -> None:
        self.vm = validate_public_vm_name(vm)
        self.lume_vm = to_lume_vm_name(self.vm)
        self.debug = debug

    def for_vm(self, vm: str) -> VmController:
        return type(self)(vm, self.debug)

    @classmethod
    def list_vms(cls, *, debug: bool = False) -> list[lume.VmInfo]:
        try:
            infos = lume.list_vms(debug=debug)
        except lume.LumeError as error:
            raise click.ClickException(str(error)) from None
        public_infos: list[lume.VmInfo] = []
        for info in infos:
            public_name = to_public_vm_name(info.name)
            if public_name is None:
                continue
            public_infos.append(
                lume.VmInfo(
                    name=public_name,
                    status=info.status,
                    ip_address=info.ip_address,
                    vnc_url=info.vnc_url,
                )
            )
        return public_infos

    def debug_log(self, message: str) -> None:
        if self.debug:
            click.echo(message, err=True)

    def get_vm(self) -> lume.VmInfo:
        info = self._get_lume_vm_info(self.lume_vm)
        if info is None:
            raise click.ClickException(f"VM not found: {self.vm}")
        return self._public_vm_info(info)

    def _get_lume_vm_info(self, name: str) -> lume.VmInfo | None:
        try:
            return lume.get_vm_info(name, debug=self.debug)
        except lume.LumeError as error:
            raise click.ClickException(str(error)) from None

    def get_running_vm(self) -> RunningVm:
        info = self.get_vm()
        return self._running_vm_from_info(info)

    def format_vm_info(self, info: lume.VmInfo) -> list[str]:
        lines = [f"name: {info.name}", f"status: {info.status}"]
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

    def clone(self, dest: str) -> None:
        dest = validate_public_vm_name(dest)
        if self.vm == dest:
            raise click.ClickException(
                f"Source and destination VM must be different: {self.vm}"
            )
        source_info = self.get_vm()
        if source_info.status != "stopped":
            raise click.ClickException(
                f"Source VM must be stopped before cloning: {self.vm} ({source_info.status})"
            )
        dest_lume_vm = to_lume_vm_name(dest)
        if self._get_lume_vm_info(dest_lume_vm) is not None:
            raise click.ClickException(f"Destination VM already exists: {dest}")
        try:
            lume.clone_vm(self.lume_vm, dest_lume_vm, debug=self.debug)
        except lume.LumeError as error:
            raise click.ClickException(str(error)) from None
        self._warm_cloned_vm(dest)

    def rename(self, dest: str) -> None:
        self.clone(dest)
        self.delete()

    def delete(self) -> None:
        info = self.get_vm()
        if info.status != "stopped":
            raise click.ClickException(
                f"VM must be stopped before deleting: {self.vm} ({info.status})"
            )
        try:
            lume.delete_vm(self.lume_vm, debug=self.debug)
        except lume.LumeError as error:
            raise click.ClickException(str(error)) from None

    def _warm_cloned_vm(self, dest: str) -> None:
        clone = self.for_vm(dest)
        try:
            # Fresh Tahoe clones can show Apple Account setup on first boot.
            # Boot once so macOS can settle that clone state, then leave the VM
            # stopped for callers.
            clone.start(require_talon=False)
            clone.stop()
        except click.ClickException as error:
            try:
                clone.stop()
            except click.ClickException as stop_error:
                self.debug_log(f"clone warm-up cleanup stop failed: {stop_error}")
            raise click.ClickException(
                f"Cloned VM was created, but warm-up failed: {error}"
            ) from None

    def start(self, *, require_talon: bool = True) -> RunningVm:
        info = self.get_vm()
        launch = None
        try:
            if info.status == "running":
                ready_info = info
            else:
                if info.status != "stopped":
                    raise click.ClickException(
                        f"VM is not stopped: {self.vm} ({info.status})"
                    )
                launch = lume.spawn_vm(self.lume_vm, debug=self.debug)
                ready_info = self._public_vm_info(
                    lume.wait_for_running_vm(
                        self.lume_vm,
                        timeout=START_TIMEOUT_SECONDS,
                        debug=self.debug,
                        launch=launch,
                    )
                )
            running_vm = self._running_vm_from_info(ready_info)
            running_vm.probe_ssh(timeout=SSH_TIMEOUT_SECONDS)
            running_vm.prevent_idle_lock()
            if require_talon:
                running_vm.ensure_talon_running()
        except click.ClickException:
            raise
        except lume.LumeError as error:
            if launch is not None and launch.process.poll() is None:
                self._cleanup_failed_start()
            raise click.ClickException(self._format_start_error(str(error))) from None
        except (RemoteCommandError, TransportError) as error:
            if launch is not None and launch.process.poll() is None:
                self._cleanup_failed_start()
            raise click.ClickException(str(error)) from None

        if launch is not None:
            lume.cleanup_launch_log(launch.log_path)
        return running_vm

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
            lume.stop_vm(self.lume_vm, debug=self.debug)
            lume.wait_for_status(
                self.lume_vm, "stopped", timeout=60.0, debug=self.debug
            )
        except lume.LumeError as error:
            self.debug_log(f"graceful stop failed: {error}")
            try:
                lume.force_stop_vm(self.lume_vm, debug=self.debug)
                lume.wait_for_status(
                    self.lume_vm, "stopped", timeout=20.0, debug=self.debug
                )
            except lume.LumeError as force_error:
                raise click.ClickException(str(force_error)) from None

    def _cleanup_failed_start(self) -> None:
        self.debug_log("start failed; stopping VM")
        try:
            lume.stop_vm(self.lume_vm, debug=self.debug)
            lume.wait_for_status(
                self.lume_vm, "stopped", timeout=30.0, debug=self.debug
            )
        except lume.LumeError as error:
            self.debug_log(f"cleanup stop failed: {error}")

    def _format_start_error(self, message: str) -> str:
        lower_message = message.lower()
        should_hint = any(
            needle in lower_message
            for needle in (
                "capacity",
                "limit",
                "maximum",
                "resource",
                "virtualization",
                "too many",
            )
        )
        if not should_hint:
            try:
                running_count = sum(
                    1
                    for info in lume.list_vms(debug=self.debug)
                    if info.status == "running"
                )
            except lume.LumeError:
                running_count = 0
            should_hint = running_count >= 2
        if should_hint and CONCURRENCY_LIMIT_HINT not in message:
            return f"{message}\nHINT {CONCURRENCY_LIMIT_HINT}"
        return message

    def _public_vm_info(self, info: lume.VmInfo) -> lume.VmInfo:
        public_name = to_public_vm_name(info.name) or info.name
        return lume.VmInfo(
            name=public_name,
            status=info.status,
            ip_address=info.ip_address,
            vnc_url=info.vnc_url,
        )

    def _running_vm_from_info(self, info: lume.VmInfo) -> RunningVm:
        if info.status != "running" or not info.ip_address:
            raise click.ClickException(f"VM is not running: {self.vm}")
        return RunningVm(
            name=info.name,
            ip_address=info.ip_address,
            debug=self.debug,
            vnc_url=info.vnc_url,
        )
