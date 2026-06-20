from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import click
from vncdotool import api as vnc_api

from .transfer import TransferService
from .vm import RemoteCommandError, RunningVm, TransportError

VNC_CAPTURE_TIMEOUT_SECONDS = 30.0


class TalonClient:
    def __init__(
        self, running_vm: RunningVm, transfer_service: TransferService
    ) -> None:
        self.running_vm = running_vm
        self.transfer_service = transfer_service

    def repl(self, code: str) -> None:
        self.running_vm.wait_for_talon_repl()
        result = self.running_vm.run_repl(
            f"exec({code!r})\n",
            stream_output=True,
        )
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def mimic(self, command: str) -> None:
        self.running_vm.wait_for_talon_repl()
        result = self.running_vm.run_repl(
            f"mimic({command!r})\n",
        )
        if result.returncode:
            raise click.exceptions.Exit(result.returncode)

    def capture_screenshot(self, filepath: Path, *, vnc: bool = False) -> None:
        filepath = self.transfer_service.normalize_local_output_path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        if vnc:
            self._capture_with_vnc(filepath)
            return

        suffix = filepath.suffix if filepath.suffix else ".png"
        remote_path = f"/tmp/talonbox-screenshot-{uuid.uuid4().hex}{suffix}"
        try:
            self.running_vm.wait_for_talon_repl()
            result = self._capture_with_talon(remote_path)
            if result.returncode:
                raise click.exceptions.Exit(result.returncode)
            self.running_vm.download(remote_path, filepath)
        except (RemoteCommandError, TransportError) as error:
            raise click.ClickException(str(error)) from None
        finally:
            try:
                self.running_vm.run_shell(
                    f'rm -f "{remote_path}"',
                )
            except (RemoteCommandError, TransportError):
                pass

    def _capture_with_talon(self, remote_path: str) -> subprocess.CompletedProcess[str]:
        if remote_path.endswith(".ppm"):
            return self._capture_with_talon_ppm(remote_path)

        code = "\n".join(
            [
                "from talon import screen",
                f"path = {remote_path!r}",
                "img = screen.capture_rect(screen.main().rect, retina=False)",
                "img.save(path) if hasattr(img, 'save') else img.write_file(path)",
                "print(path)",
                "",
            ]
        )
        return self.running_vm.run_repl(f"exec({code!r})\n")

    def _capture_with_talon_ppm(
        self, remote_path: str
    ) -> subprocess.CompletedProcess[str]:
        code = "\n".join(
            [
                "from pathlib import Path",
                "from talon import screen",
                f"path = {remote_path!r}",
                "img = screen.capture_rect(screen.main().rect, retina=False)",
                "pixels = img.read_pixels(0, 0, img.width, img.height)",
                "expected = img.width * img.height * 4",
                "if len(pixels) != expected:",
                "    raise RuntimeError(f'unexpected screenshot byte count: {len(pixels)} != {expected}')",
                "rgb = bytearray(img.width * img.height * 3)",
                "for source in range(0, len(pixels), 4):",
                "    target = (source // 4) * 3",
                "    rgb[target] = pixels[source + 2]",
                "    rgb[target + 1] = pixels[source + 1]",
                "    rgb[target + 2] = pixels[source]",
                "header = f'P6\\n{img.width} {img.height}\\n255\\n'.encode()",
                "Path(path).write_bytes(header + rgb)",
                "print(path)",
                "",
            ]
        )
        return self.running_vm.run_repl(f"exec({code!r})\n")

    def _capture_with_vnc(self, filepath: Path) -> None:
        if not self.running_vm.vnc_url:
            raise click.ClickException("VM does not expose a VNC URL.")
        server, password = self._parse_vnc_url(self.running_vm.vnc_url)
        connect = cast(Any, vnc_api.connect)
        try:
            with connect(
                server,
                password=password,
                timeout=VNC_CAPTURE_TIMEOUT_SECONDS,
            ) as client:
                client.captureScreen(str(filepath))
        except Exception as error:
            raise click.ClickException(f"VNC screenshot failed: {error}") from None
        finally:
            vnc_api.shutdown()

    def _parse_vnc_url(self, vnc_url: str) -> tuple[str, str | None]:
        parsed = urlparse(vnc_url)
        if parsed.scheme != "vnc" or not parsed.hostname:
            raise click.ClickException(f"Invalid VNC URL for VM: {vnc_url}")
        try:
            port = parsed.port
        except ValueError as error:
            raise click.ClickException(f"Invalid VNC URL for VM: {vnc_url}") from error

        server = parsed.hostname if port is None else f"{parsed.hostname}::{port}"
        password = unquote(parsed.password) if parsed.password is not None else None
        return server, password
