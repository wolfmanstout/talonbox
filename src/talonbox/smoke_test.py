from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable
from pathlib import Path

import click

from . import lume
from .talon_client import TalonClient
from .transfer import HOST_OUTPUT_ROOT, TransferService
from .vm import RunningVm, VmController


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
        screenshot_path = artifact_dir / "screenshot-after-visual-change.png"
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
            assert isinstance(info, lume.VmInfo)
            if clone and info.status != "stopped":
                raise click.ClickException(
                    f"Source VM must be stopped before smoke-test: {self.vm_controller.vm} ({info.status})"
                )

            if clone:
                self.run_step(
                    "Clone the source VM for smoke-test",
                    lambda: self.vm_controller.clone(temp_vm_name),
                    success_message=f"Temporary VM cloned: {temp_vm_name}",
                )
                cloned = True

            running_vm = self.run_step(
                "Start the smoke-test VM" if clone else "Start the VM",
                smoke_vm_controller.start,
                success_message="Smoke-test VM started." if clone else "VM started.",
            )
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
                lambda: smoke_vm_controller.restart_talon(
                    wipe_user_dir=False,
                    clean_logs=True,
                ),
                success_message="Talon restarted after upload.",
            )
            self.run_step(
                "Run mimic for 'talonbox smoke test'",
                lambda: talon_client.mimic("talonbox smoke test"),
                success_message="mimic succeeded.",
            )
            self.run_step(
                "Verify the guest smoke-test marker",
                lambda: self.verify_marker(running_vm, marker_path, token),
                success_message="Guest marker verified.",
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
                "Verify the screenshots changed after the guest visual change",
                lambda: self.verify_screenshots_differ(
                    baseline_screenshot_path, screenshot_path
                ),
                success_message="Screenshots changed after the guest visual change.",
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
        returncode = transfer_service.rsync(
            [
                "-a",
                f"{bundle_dir}/",
                f"{transfer_service.running_vm.name}:/Users/lume/.talon/user/talonbox_smoke_test/",
            ]
        )
        if returncode:
            raise click.ClickException(f"rsync failed with exit code {returncode}")

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

    def trigger_visual_change(self, talon_client: TalonClient) -> None:
        talon_client.mimic("talonbox smoke visual test")

    def _build_transfer_service(self, running_vm: RunningVm) -> TransferService:
        return TransferService(running_vm)

    def _build_talon_client(
        self, running_vm: RunningVm, transfer_service: TransferService
    ) -> TalonClient:
        return TalonClient(running_vm, transfer_service)

    def verify_screenshots_differ(self, before_path: Path, after_path: Path) -> None:
        if before_path.read_bytes() == after_path.read_bytes():
            raise click.ClickException(
                "Smoke test screenshots did not change after the guest visual change."
            )

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
