from __future__ import annotations

import shlex
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import click

from . import tart
from .talon_client import TalonClient
from .transfer import HOST_OUTPUT_ROOT, TransferService, parse_rsync_args
from .vm import RemoteCommandError, RunningVm, StartResult, TransportError, VmController

MARKER_MIMIC_ATTEMPTS = 10
MARKER_MIMIC_RETRY_DELAY_SECONDS = 1.0
SMOKE_MARKER_MAGENTA = (255, 0, 255)
SMOKE_MARKER_GREEN = (0, 255, 0)
SMOKE_MARKER_MIN_COLOR_RATIO = 0.05
DESKTOP_PROBE_TIMEOUT_SECONDS = 30
DESKTOP_PROBE_SETTLE_SECONDS = 1.0
DESKTOP_CAPTURE_MAX_MEAN_ABS_DIFF = 4.0
DESKTOP_CAPTURE_MAX_MISMATCH_RATIO = 0.02
DESKTOP_CAPTURE_MISMATCH_CHANNEL_THRESHOLD = 40
GUEST_SMOKE_BUNDLE_PATH = "/Users/admin/.talon/user/talonbox_smoke_test"


class MimicClient(Protocol):
    def mimic(self, command: str) -> None: ...


class SmokeTestRunner:
    def __init__(
        self,
        vm_controller: VmController,
        *,
        host_output_root: Path = HOST_OUTPUT_ROOT,
    ) -> None:
        self.vm_controller = vm_controller
        self.host_output_root = host_output_root
        self._hint_screenshot: Callable[[], Path | None] | None = None
        self._hint_vm_controller: VmController | None = None

    def run(
        self,
        *,
        clone: bool = True,
    ) -> None:
        artifact_dir = self.host_output_root / f"talonbox-smoke-test-{uuid.uuid4().hex}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        baseline_screenshot_path = artifact_dir / "screenshot-before-visual-change.png"
        desktop_probe_talon_path = artifact_dir / "screenshot-desktop-probe-talon.ppm"
        desktop_probe_vnc_path = artifact_dir / "screenshot-desktop-probe-vnc.ppm"
        desktop_probe_talon_png_path = (
            artifact_dir / "screenshot-desktop-probe-talon.png"
        )
        desktop_probe_vnc_png_path = artifact_dir / "screenshot-desktop-probe-vnc.png"
        screenshot_path = artifact_dir / "screenshot-after-visual-change.png"
        marker_ppm_path = artifact_dir / "screenshot-after-visual-change.ppm"
        bundle_dir = artifact_dir / "bundle"
        marker_path = f"/tmp/talonbox-smoke-test-marker-{uuid.uuid4().hex}.txt"
        token = uuid.uuid4().hex
        started = False
        cloned = False
        temp_vm_name = f"smoke-test-{uuid.uuid4().hex}"
        smoke_vm_controller = (
            self.vm_controller.for_vm(temp_vm_name) if clone else self.vm_controller
        )

        def hint_screenshot() -> Path | None:
            if screenshot_path.exists():
                return screenshot_path
            if desktop_probe_vnc_path.exists():
                return desktop_probe_vnc_path
            if desktop_probe_talon_path.exists():
                return desktop_probe_talon_path
            if baseline_screenshot_path.exists():
                return baseline_screenshot_path
            return None

        self._hint_screenshot = hint_screenshot
        self._hint_vm_controller = smoke_vm_controller
        self.log("ARTIFACT", artifact_dir)

        try:
            info = self.run_step(
                "Inspect VM status",
                self.vm_controller.get_vm,
                success_message="VM status checked.",
            )
            assert isinstance(info, tart.VmInfo)
            if clone and info.status != "stopped":
                raise click.ClickException(
                    f"Source VM must be stopped before smoke-test: {self.vm_controller.vm} ({info.status}). "
                    f"Run `talonbox stop --shutdown {self.vm_controller.vm}` first."
                )

            if clone:
                self.run_step(
                    "Clone the source VM for smoke-test",
                    lambda: self.vm_controller.clone(temp_vm_name),
                    success_message=f"Temporary VM cloned: {temp_vm_name}",
                )
                cloned = True

            start_result = self.run_step(
                "Start the smoke-test VM" if clone else "Start the VM",
                smoke_vm_controller.start,
                success_message="Smoke-test VM started." if clone else "VM started.",
            )
            assert isinstance(start_result, StartResult)
            running_vm = start_result.running_vm
            assert isinstance(running_vm, RunningVm)
            started = True
            transfer_service = self._build_transfer_service(running_vm)
            talon_client = self._build_talon_client(running_vm, transfer_service)

            self.run_step(
                "Write the temporary Talon smoke-test bundle",
                lambda: self.write_bundle(bundle_dir, marker_path, token),
                success_message="Temporary Talon bundle written.",
            )
            self.run_step(
                "Upload the Talon smoke-test bundle with rsync",
                lambda: self.upload_bundle(transfer_service, bundle_dir),
                success_message="Temporary Talon bundle uploaded.",
            )
            self.run_step(
                "Restart Talon to load the uploaded bundle",
                smoke_vm_controller.restart_talon,
                success_message="Talon restarted after upload.",
            )
            self.run_step(
                "Show a non-Talon desktop capture probe",
                lambda: self.start_desktop_capture_probe(running_vm, token),
                success_message="Desktop capture probe shown.",
            )
            self.run_step(
                "Capture the desktop probe with Talon",
                lambda: talon_client.capture_screenshot(desktop_probe_talon_path),
                success_message="Talon desktop probe screenshot captured.",
            )
            self.run_step(
                "Capture the desktop probe with VNC",
                lambda: talon_client.capture_screenshot(
                    desktop_probe_vnc_path,
                    vnc=True,
                ),
                success_message="VNC desktop probe screenshot captured.",
            )
            self.run_step(
                "Capture a PNG copy of the desktop probe with Talon",
                lambda: talon_client.capture_screenshot(desktop_probe_talon_png_path),
                success_message="Talon desktop probe PNG captured.",
            )
            self.run_step(
                "Capture a PNG copy of the desktop probe with VNC",
                lambda: talon_client.capture_screenshot(
                    desktop_probe_vnc_png_path,
                    vnc=True,
                ),
                success_message="VNC desktop probe PNG captured.",
            )
            self.run_step(
                "Close the desktop capture probe",
                lambda: self.close_desktop_capture_probe(running_vm),
                success_message="Desktop capture probe closed.",
            )
            self.run_step(
                "Verify Talon can capture the complete desktop",
                lambda: self.verify_talon_capture_matches_vnc(
                    desktop_probe_talon_path,
                    desktop_probe_vnc_path,
                ),
                success_message="Talon desktop capture matched VNC.",
            )
            self.run_step(
                "Run mimic for 'talonbox smoke test' until the marker is verified",
                lambda: self.run_marker_mimic_until_verified(
                    talon_client,
                    running_vm,
                    marker_path,
                    token,
                ),
                success_message="mimic created the guest marker.",
            )
            self.run_step(
                "Capture a baseline screenshot",
                lambda: talon_client.capture_screenshot(baseline_screenshot_path),
                success_message="Baseline screenshot captured.",
            )
            self.run_step(
                "Validate the baseline screenshot artifact",
                lambda: self.validate_screenshot(baseline_screenshot_path),
                success_message="Baseline screenshot artifact validated.",
            )
            self.run_step(
                "Trigger a non-modal visible guest change",
                lambda: self.trigger_visual_change(talon_client),
                success_message="Visible guest change triggered.",
            )
            self.run_step(
                "Capture a second screenshot after the guest visual change",
                lambda: talon_client.capture_screenshot(screenshot_path),
                success_message="Second screenshot captured.",
            )
            self.run_step(
                "Validate the second screenshot artifact",
                lambda: self.validate_screenshot(screenshot_path),
                success_message="Second screenshot artifact validated.",
            )
            self.run_step(
                "Capture a PPM screenshot for visual marker validation",
                lambda: talon_client.capture_screenshot(marker_ppm_path),
                success_message="PPM marker validation screenshot captured.",
            )
            self.run_step(
                "Verify the Talon visual marker appeared",
                lambda: self.verify_visual_marker_present(marker_ppm_path),
                success_message="Talon visual marker appeared.",
            )
            if not clone:
                self.run_step(
                    "Remove the temporary Talon smoke-test files",
                    lambda: self.cleanup_guest_artifacts(running_vm, marker_path),
                    success_message="Temporary Talon smoke-test files removed.",
                )
        except click.ClickException as error:
            self._fail(str(error), screenshot_path=hint_screenshot())
        except click.exceptions.Exit:
            raise
        finally:
            self._hint_screenshot = None
            self._hint_vm_controller = None
            if clone and started:
                self.run_step(
                    "Stop the smoke-test VM",
                    smoke_vm_controller.stop,
                    success_message="Smoke-test VM stopped.",
                )
            if cloned:
                self.run_step(
                    "Delete the smoke-test VM",
                    smoke_vm_controller.delete,
                    success_message="Smoke-test VM deleted.",
                )

        self.log("PASS", "Smoke test completed successfully.")

    def write_bundle(self, bundle_dir: Path, marker_path: str, token: str) -> None:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "talonbox_smoke_test.talon").write_text(
            "\n".join(
                [
                    "-",
                    "talonbox smoke test:",
                    "    user.talonbox_smoke_test()",
                    "talonbox smoke visual test:",
                    "    user.talonbox_smoke_test_show_visual_marker()",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bundle_dir / "talonbox_smoke_test.py").write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "",
                    "from talon import Module, cron, screen",
                    "from talon.canvas import Canvas",
                    "from talon.types import Rect",
                    "",
                    "mod = Module()",
                    "_smoke_canvas = None",
                    "_smoke_cleanup_job = None",
                    "",
                    "",
                    "def _close_smoke_canvas() -> None:",
                    "    global _smoke_canvas, _smoke_cleanup_job",
                    "    if _smoke_cleanup_job is not None:",
                    "        cron.cancel(_smoke_cleanup_job)",
                    "        _smoke_cleanup_job = None",
                    "    if _smoke_canvas is not None:",
                    "        _smoke_canvas.close()",
                    "        _smoke_canvas = None",
                    "",
                    "",
                    "def _draw_smoke_marker(canvas) -> None:",
                    "    rect = canvas.rect",
                    "    canvas.paint.style = canvas.paint.Style.FILL",
                    '    canvas.paint.color = "FF00FF"',
                    "    canvas.draw_rect(rect)",
                    '    canvas.paint.color = "00FF00"',
                    "    canvas.draw_rect(",
                    "        Rect(",
                    "            rect.x + rect.width * 0.1,",
                    "            rect.y + rect.height * 0.1,",
                    "            rect.width * 0.8,",
                    "            rect.height * 0.8,",
                    "        )",
                    "    )",
                    '    canvas.paint.color = "000000"',
                    "    canvas.paint.textsize = 48",
                    f"    canvas.draw_text({('talonbox smoke test ' + token)!r}, rect.x + 80, rect.y + 120)",
                    "",
                    "@mod.action_class",
                    "class Actions:",
                    "    def talonbox_smoke_test() -> None:",
                    '        """Write the talonbox smoke-test marker file."""',
                    f"        Path({marker_path!r}).write_text({token!r}, encoding='utf-8')",
                    "",
                    "    def talonbox_smoke_test_show_visual_marker() -> None:",
                    '        """Show a large Talon canvas marker for screenshot validation."""',
                    "        global _smoke_canvas, _smoke_cleanup_job",
                    "        _close_smoke_canvas()",
                    "        _smoke_canvas = Canvas.from_screen(screen.main())",
                    "        _smoke_canvas.blocks_mouse = False",
                    "        _smoke_canvas.focused = False",
                    "        _smoke_canvas.cursor_visible = True",
                    '        _smoke_canvas.register("draw", _draw_smoke_marker)',
                    "        _smoke_canvas.freeze()",
                    '        _smoke_cleanup_job = cron.after("10s", _close_smoke_canvas)',
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def upload_bundle(
        self, transfer_service: TransferService, bundle_dir: Path
    ) -> None:
        parsed_args = parse_rsync_args(
            [
                "-a",
                f"{bundle_dir}/",
                f"{transfer_service.running_vm.name}:{GUEST_SMOKE_BUNDLE_PATH}/",
            ]
        )
        returncode = transfer_service.rsync(parsed_args)
        if returncode:
            raise click.ClickException(f"rsync failed with exit code {returncode}")

    def cleanup_guest_artifacts(self, running_vm: RunningVm, marker_path: str) -> None:
        running_vm.run_shell(["rm", "-rf", GUEST_SMOKE_BUNDLE_PATH])
        running_vm.run_shell(["rm", "-f", marker_path])

    def verify_marker(
        self, running_vm: RunningVm, marker_path: str, token: str
    ) -> None:
        result = running_vm.run_shell(
            ["cat", marker_path],
            check=False,
        )
        if result.returncode != 0:
            raise click.ClickException(
                result.stderr.strip()
                or result.stdout.strip()
                or f"Smoke test marker was not created: {marker_path}"
            )
        if result.stdout.strip() != token:
            raise click.ClickException(
                f"Smoke test marker contents did not match expected token: {marker_path}"
            )

    def run_marker_mimic_until_verified(
        self,
        talon_client: MimicClient,
        running_vm: RunningVm,
        marker_path: str,
        token: str,
        *,
        attempts: int = MARKER_MIMIC_ATTEMPTS,
        retry_delay: float = MARKER_MIMIC_RETRY_DELAY_SECONDS,
    ) -> None:
        last_error: click.ClickException | None = None
        for attempt in range(1, attempts + 1):
            talon_client.mimic("talonbox smoke test")
            try:
                self.verify_marker(running_vm, marker_path, token)
            except click.ClickException as error:
                last_error = error
                if attempt < attempts:
                    time.sleep(retry_delay)
                    continue
                break
            else:
                return

        message = (
            last_error.message
            if last_error is not None
            else f"Smoke test marker was not created: {marker_path}"
        )
        diagnosis = self.diagnose_mimic_failure(running_vm, marker_path, token)
        raise click.ClickException(
            f"Smoke test marker was not verified after {attempts} mimic attempts: {message}"
            f"{diagnosis}"
        )

    def diagnose_mimic_failure(
        self, running_vm: RunningVm, marker_path: str, token: str
    ) -> str:
        code = "\n".join(
            [
                "from pathlib import Path",
                "from talon import actions",
                f"path = Path({marker_path!r})",
                "path.unlink(missing_ok=True)",
                "actions.user.talonbox_smoke_test()",
                f"print('talonbox_direct_action_ok=' + str(path.exists() and path.read_text(encoding='utf-8') == {token!r}))",
                "",
            ]
        )
        try:
            result = running_vm.run_repl(f"exec({code!r})\n")
        except (RemoteCommandError, TransportError):
            return (
                " The smoke-test command bundle could not be checked directly. "
                "Inspect Talon logs and confirm a speech model is installed and selected."
            )
        if result.returncode == 0 and "talonbox_direct_action_ok=True" in (
            result.stdout or ""
        ):
            return (
                " The smoke-test action is loaded and works when called directly, "
                "but Talon's mimic() did not dispatch it. Confirm Speech Recognition "
                "has an installed, selected model in the Talon menu, then rerun smoke-test."
            )
        return (
            " Confirm Talon loaded the smoke-test command bundle, and confirm Speech "
            "Recognition has an installed, selected model in the Talon menu."
        )

    def validate_screenshot(self, path: Path) -> None:
        if not path.exists():
            raise click.ClickException(f"Smoke test screenshot was not created: {path}")
        if path.stat().st_size <= 0:
            raise click.ClickException(f"Smoke test screenshot was empty: {path}")
        with path.open("rb") as handle:
            signature = handle.read(8)
        if signature != b"\x89PNG\r\n\x1a\n":
            raise click.ClickException(
                f"Smoke test screenshot was not a PNG file: {path}"
            )

    def start_desktop_capture_probe(self, running_vm: RunningVm, token: str) -> None:
        script = (
            f'display dialog "talonbox desktop capture probe {token}" '
            f'buttons {{"OK"}} giving up after {DESKTOP_PROBE_TIMEOUT_SECONDS}'
        )
        running_vm.run_shell(
            "nohup osascript -e "
            f"{shlex.quote(script)} "
            ">/tmp/talonbox-desktop-capture-probe.log 2>&1 &"
        )
        time.sleep(DESKTOP_PROBE_SETTLE_SECONDS)

    def close_desktop_capture_probe(self, running_vm: RunningVm) -> None:
        running_vm.run_shell(
            "pkill -f 'talonbox desktop capture [p]robe' >/dev/null 2>&1 || true"
        )

    def trigger_visual_change(self, talon_client: TalonClient) -> None:
        talon_client.repl(
            "\n".join(
                [
                    "from talon import actions",
                    "actions.user.talonbox_smoke_test_show_visual_marker()",
                ]
            )
        )

    def _build_transfer_service(self, running_vm: RunningVm) -> TransferService:
        return TransferService(running_vm)

    def _build_talon_client(
        self, running_vm: RunningVm, transfer_service: TransferService
    ) -> TalonClient:
        return TalonClient(running_vm, transfer_service)

    def verify_visual_marker_present(self, path: Path) -> None:
        total, magenta, green = self.count_marker_pixels(path)
        minimum = max(1, int(total * SMOKE_MARKER_MIN_COLOR_RATIO))
        if magenta < minimum or green < minimum:
            raise click.ClickException(
                "Smoke test screenshot did not contain the Talon visual marker: "
                f"{path} (magenta pixels: {magenta}, green pixels: {green}, "
                f"minimum expected for each: {minimum})"
            )

    def verify_talon_capture_matches_vnc(
        self, talon_path: Path, vnc_path: Path
    ) -> None:
        talon_width, talon_height, talon_rgb = self.read_ppm_rgb(talon_path)
        vnc_width, vnc_height, vnc_rgb = self.read_ppm_rgb(vnc_path)
        if talon_width <= 0 or talon_height <= 0:
            raise click.ClickException(
                f"Talon desktop capture had invalid dimensions: {talon_path}"
            )
        if vnc_width <= 0 or vnc_height <= 0:
            raise click.ClickException(
                f"VNC desktop capture had invalid dimensions: {vnc_path}"
            )
        total_pixels = talon_width * talon_height
        total_channel_diff = 0
        mismatched_pixels = 0
        for y in range(talon_height):
            source_y = min(vnc_height - 1, int((y + 0.5) * vnc_height / talon_height))
            for x in range(talon_width):
                source_x = min(
                    vnc_width - 1,
                    int((x + 0.5) * vnc_width / talon_width),
                )
                talon_offset = (y * talon_width + x) * 3
                vnc_offset = (source_y * vnc_width + source_x) * 3
                red_diff = abs(talon_rgb[talon_offset] - vnc_rgb[vnc_offset])
                green_diff = abs(talon_rgb[talon_offset + 1] - vnc_rgb[vnc_offset + 1])
                blue_diff = abs(talon_rgb[talon_offset + 2] - vnc_rgb[vnc_offset + 2])
                total_channel_diff += red_diff + green_diff + blue_diff
                if (
                    max(red_diff, green_diff, blue_diff)
                    > DESKTOP_CAPTURE_MISMATCH_CHANNEL_THRESHOLD
                ):
                    mismatched_pixels += 1

        mean_abs_diff = total_channel_diff / (total_pixels * 3)
        mismatch_ratio = mismatched_pixels / total_pixels
        if (
            mean_abs_diff > DESKTOP_CAPTURE_MAX_MEAN_ABS_DIFF
            or mismatch_ratio > DESKTOP_CAPTURE_MAX_MISMATCH_RATIO
        ):
            raise click.ClickException(
                "Talon desktop capture did not match the VNC framebuffer. "
                "Talon may be missing Screen Recording permission. "
                f"mean absolute channel difference: {mean_abs_diff:.2f} "
                f"(maximum {DESKTOP_CAPTURE_MAX_MEAN_ABS_DIFF:.2f}); "
                f"mismatched pixel ratio: {mismatch_ratio:.2%} "
                f"(maximum {DESKTOP_CAPTURE_MAX_MISMATCH_RATIO:.2%}); "
                f"Talon capture: {talon_path}; VNC capture: {vnc_path}; "
                f"PNG copies should be saved next to those PPM files."
            )

    def count_marker_pixels(self, path: Path) -> tuple[int, int, int]:
        width, height, rgb = self.read_ppm_rgb(path)
        magenta = 0
        green = 0
        for offset in range(0, len(rgb), 3):
            color = tuple(rgb[offset : offset + 3])
            if color == SMOKE_MARKER_MAGENTA:
                magenta += 1
            elif color == SMOKE_MARKER_GREEN:
                green += 1
        return width * height, magenta, green

    def read_ppm_rgb(self, path: Path) -> tuple[int, int, bytes]:
        data = path.read_bytes()
        tokens: list[bytes] = []
        offset = 0
        while len(tokens) < 4:
            while offset < len(data) and chr(data[offset]).isspace():
                offset += 1
            if offset >= len(data):
                raise click.ClickException(f"Smoke test PPM was truncated: {path}")
            if data[offset : offset + 1] == b"#":
                newline = data.find(b"\n", offset)
                if newline == -1:
                    raise click.ClickException(f"Smoke test PPM was truncated: {path}")
                offset = newline + 1
                continue
            end = offset
            while end < len(data) and not chr(data[end]).isspace():
                end += 1
            tokens.append(data[offset:end])
            offset = end

        if tokens[0] != b"P6":
            raise click.ClickException(
                f"Smoke test screenshot was not a P6 PPM file: {path}"
            )
        try:
            width = int(tokens[1])
            height = int(tokens[2])
            max_value = int(tokens[3])
        except ValueError:
            raise click.ClickException(
                f"Smoke test PPM header was invalid: {path}"
            ) from None
        if width <= 0 or height <= 0 or max_value != 255:
            raise click.ClickException(f"Smoke test PPM header was invalid: {path}")
        if offset >= len(data) or not chr(data[offset]).isspace():
            raise click.ClickException(f"Smoke test PPM header was invalid: {path}")
        offset += 1
        pixels = data[offset:]
        expected_size = width * height * 3
        if len(pixels) != expected_size:
            raise click.ClickException(
                f"Smoke test PPM pixel data had {len(pixels)} bytes; expected {expected_size}: {path}"
            )
        return width, height, pixels

    def run_step(
        self,
        name: str,
        action: Callable[[], object],
        *,
        success_message: str | None = None,
    ) -> object:
        self.log("STEP", name)
        try:
            result = action()
        except click.ClickException as error:
            self._fail(f"{name}: {error.message}")
        except click.exceptions.Exit as error:
            exit_code = getattr(error, "exit_code", 1)
            self._fail(f"{name}: command exited with status {exit_code}")
        except Exception as error:
            self._fail(f"{name}: {error}")
        else:
            self.log("PASS", success_message or name)
            return result

    def log(self, status: str, message: str | Path) -> None:
        click.echo(f"{status} {message}")

    def _fail(self, message: str, *, screenshot_path: Path | None = None) -> None:
        if screenshot_path is None and self._hint_screenshot is not None:
            screenshot_path = self._hint_screenshot()
        self.log("FAIL", message)
        self._print_hints(screenshot_path=screenshot_path)
        raise click.exceptions.Exit(1)

    def _print_hints(self, *, screenshot_path: Path | None) -> None:
        click.echo("HINT rerun with --debug for command traces and transport details.")
        if self.vm_controller.debug:
            click.echo(
                "HINT --debug is already enabled; inspect the command trace above."
            )
        click.echo(
            "HINT inspect guest logs at ~/.talon/talon.log and /tmp/talonbox-talon.log."
        )
        if self._hint_vm_controller is not None:
            try:
                info = self._hint_vm_controller.get_vm()
            except click.ClickException:
                info = None
            if info is not None and info.status == "running" and info.vnc_url:
                click.echo(
                    f"HINT open the VM over VNC with `talonbox open {self._hint_vm_controller.vm}`."
                )
                click.echo(f"HINT or run `open {shlex.quote(info.vnc_url)}`.")
        if screenshot_path is not None:
            click.echo(f"HINT inspect the saved screenshot at {screenshot_path}.")
