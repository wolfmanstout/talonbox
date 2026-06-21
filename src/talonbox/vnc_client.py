from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import click
from vncdotool import api as vnc_api

from .transfer import TransferService
from .vm import RunningVm

VNC_CAPTURE_TIMEOUT_SECONDS = 30.0
VNC_INPUT_TIMEOUT_SECONDS = 30.0

VNC_MOUSE_BUTTONS = {
    "left": 1,
    "middle": 2,
    "right": 3,
}


class VncClient:
    def __init__(
        self, running_vm: RunningVm, transfer_service: TransferService
    ) -> None:
        self.running_vm = running_vm
        self.transfer_service = transfer_service

    def capture_screenshot(self, filepath: Path) -> None:
        filepath = self.transfer_service.normalize_local_output_path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect(timeout=VNC_CAPTURE_TIMEOUT_SECONDS) as client:
                client.captureScreen(str(filepath))
        except click.ClickException:
            raise
        except Exception as error:
            raise click.ClickException(f"VNC screenshot failed: {error}") from None
        finally:
            vnc_api.shutdown()

    def click(self, x: int, y: int, *, button: str) -> None:
        try:
            with self._connect(timeout=VNC_INPUT_TIMEOUT_SECONDS) as client:
                client.mouseMove(x, y)
                client.mousePress(VNC_MOUSE_BUTTONS[button])
        except click.ClickException:
            raise
        except Exception as error:
            raise click.ClickException(f"VNC click failed: {error}") from None
        finally:
            vnc_api.shutdown()

    def type_text(self, text: str) -> None:
        try:
            with self._connect(timeout=VNC_INPUT_TIMEOUT_SECONDS) as client:
                for key in self._type_keys(text):
                    client.keyPress(key)
        except click.ClickException:
            raise
        except Exception as error:
            raise click.ClickException(f"VNC type failed: {error}") from None
        finally:
            vnc_api.shutdown()

    def _connect(self, *, timeout: float) -> Any:
        if not self.running_vm.vnc_url:
            raise click.ClickException("VM does not expose a VNC URL.")
        server, password = self._parse_vnc_url(self.running_vm.vnc_url)
        connect = cast(Any, vnc_api.connect)
        return connect(
            server,
            password=password,
            timeout=timeout,
        )

    def _type_keys(self, text: str) -> list[str]:
        keys = []
        for char in text:
            if char == "\r":
                continue
            if char == "\n":
                keys.append("enter")
            elif char == "\t":
                keys.append("tab")
            elif char == "-":
                keys.append("minus")
            else:
                keys.append(char)
        return keys

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
