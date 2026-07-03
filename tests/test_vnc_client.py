from __future__ import annotations

from pathlib import Path

import click
import pytest

from talonbox.vnc_client import VncClient
from tests.helpers import build_service_stack


def test_vnc_client_screenshot_captures_to_normalized_local_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    vnc_client = VncClient(talon_client.running_vm, transfer_service)
    connects: list[tuple[str, str | None]] = []
    captures: list[Path] = []
    target = tmp_path / "shots" / "screen.png"
    talon_client.running_vm.vnc_url = "vnc://:secret%20words@127.0.0.1:63414"

    class FakeVncConnection:
        def __enter__(self) -> FakeVncConnection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def captureScreen(self, path: str) -> None:
            captures.append(Path(path))
            Path(path).write_bytes(b"not-a-png")

    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: tmp_path.resolve()
    )
    monkeypatch.setattr(
        "talonbox.vnc_client.vnc_api.connect",
        lambda server, password=None, timeout=None: (
            connects.append((server, password)) or FakeVncConnection()
        ),
    )

    vnc_client.capture_screenshot(target)

    assert target.parent.exists()
    assert connects == [("127.0.0.1::63414", "secret words")]
    assert captures == [target]


def test_vnc_client_screenshot_allows_repeated_captures_in_one_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    vnc_client = VncClient(talon_client.running_vm, transfer_service)
    captures: list[Path] = []
    talon_client.running_vm.vnc_url = "vnc://127.0.0.1:63414"

    class FakeVncConnection:
        def __enter__(self) -> FakeVncConnection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def captureScreen(self, path: str) -> None:
            captures.append(Path(path))
            Path(path).write_bytes(b"not-a-png")

    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: tmp_path.resolve()
    )
    monkeypatch.setattr(
        "talonbox.vnc_client.vnc_api.connect",
        lambda server, password=None, timeout=None: FakeVncConnection(),
    )
    monkeypatch.setattr(
        "talonbox.vnc_client.vnc_api.shutdown",
        lambda: pytest.fail("VNC operations must not stop Twisted's global reactor"),
    )

    vnc_client.capture_screenshot(tmp_path / "first.png")
    vnc_client.capture_screenshot(tmp_path / "second.png")

    assert captures == [tmp_path / "first.png", tmp_path / "second.png"]


def test_vnc_client_click_uses_vncdotool_button_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    vnc_client = VncClient(talon_client.running_vm, transfer_service)
    actions: list[tuple[str, int, int] | tuple[str, int]] = []
    talon_client.running_vm.vnc_url = "vnc://127.0.0.1:63414"

    class FakeVncConnection:
        def __enter__(self) -> FakeVncConnection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def mouseMove(self, x: int, y: int) -> None:
            actions.append(("move", x, y))

        def mousePress(self, button: int) -> None:
            actions.append(("press", button))

    monkeypatch.setattr(
        "talonbox.vnc_client.vnc_api.connect",
        lambda server, password=None, timeout=None: FakeVncConnection(),
    )

    vnc_client.click(123, 456, button="middle")

    assert actions == [("move", 123, 456), ("press", 2)]


def test_vnc_client_type_matches_vncdotool_type_key_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    vnc_client = VncClient(talon_client.running_vm, transfer_service)
    keys: list[str] = []
    talon_client.running_vm.vnc_url = "vnc://127.0.0.1:63414"

    class FakeVncConnection:
        def __enter__(self) -> FakeVncConnection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def keyPress(self, key: str) -> None:
            keys.append(key)

    monkeypatch.setattr(
        "talonbox.vnc_client.vnc_api.connect",
        lambda server, password=None, timeout=None: FakeVncConnection(),
    )

    vnc_client.type_text("a-\n\tb\r")

    assert keys == ["a", "minus", "enter", "tab", "b"]


@pytest.mark.parametrize(
    ("key", "expected"),
    [("enter", "enter"), ("return", "enter"), ("space", " ")],
)
def test_vnc_client_press_key_uses_vncdotool_key_mapping(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    expected: str,
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    vnc_client = VncClient(talon_client.running_vm, transfer_service)
    keys: list[str] = []
    talon_client.running_vm.vnc_url = "vnc://127.0.0.1:63414"

    class FakeVncConnection:
        def __enter__(self) -> FakeVncConnection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def keyPress(self, key: str) -> None:
            keys.append(key)

    monkeypatch.setattr(
        "talonbox.vnc_client.vnc_api.connect",
        lambda server, password=None, timeout=None: FakeVncConnection(),
    )

    vnc_client.press_key(key)

    assert keys == [expected]


def test_vnc_client_requires_vnc_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, transfer_service, talon_client = build_service_stack()
    vnc_client = VncClient(talon_client.running_vm, transfer_service)

    monkeypatch.setattr(
        transfer_service, "_host_output_root", lambda: tmp_path.resolve()
    )
    target = tmp_path / "screen.png"
    with pytest.raises(click.ClickException, match="does not expose a VNC URL"):
        vnc_client.capture_screenshot(target)


def test_vnc_client_screenshot_rejects_output_outside_tmp() -> None:
    _, transfer_service, talon_client = build_service_stack()
    vnc_client = VncClient(talon_client.running_vm, transfer_service)
    talon_client.running_vm.vnc_url = "vnc://127.0.0.1:63414"

    with pytest.raises(
        click.ClickException, match="Local output paths must stay under /tmp"
    ):
        vnc_client.capture_screenshot(Path("/private/var/guest-screen.png"))
