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

    def run(
        self,
        *,
        yes: bool,
        confirm: Callable[..., bool] = click.confirm,
    ) -> None:
        artifact_dir = self.host_output_root / f"talonbox-smoke-test-{uuid.uuid4().hex}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        baseline_screenshot_path = artifact_dir / "screenshot-before-dialog.png"
        screenshot_path = artifact_dir / "screenshot-after-dialog.png"
        bundle_dir = artifact_dir / "bundle"
        marker_path = f"/tmp/talonbox-smoke-test-marker-{uuid.uuid4().hex}.txt"
        token = uuid.uuid4().hex
        started = False

        def hint_screenshot() -> Path | None:
            if screenshot_path.exists():
                return screenshot_path
            if baseline_screenshot_path.exists():
                return baseline_screenshot_path
            return None

        self._hint_screenshot = hint_screenshot
        self.log("ARTIFACT", artifact_dir)

        try:
            info = self.run_step(
                "Inspect VM status",
                self.vm_controller.get_vm,
                success_message="VM status checked.",
            )
            assert isinstance(info, lume.VmInfo)
            if info.status not in {"running", "stopped"}:
                raise click.ClickException(
                    f"VM is not ready for smoke-test: {self.vm_controller.vm} ({info.status})"
                )

            if info.status == "running":
                message = (
                    f"VM {self.vm_controller.vm} is already running. smoke-test must stop "
                    "and restart it before continuing."
                )
                click.echo(message)
                if not yes and not confirm("Continue with smoke-test?", default=False):
                    self.log("FAIL", "smoke-test canceled by user; VM left running.")
                    raise click.exceptions.Exit(1)
                self.run_step(
                    "Stop the running VM before smoke-test",
                    self.vm_controller.stop,
                    success_message="Running VM stopped.",
                )

            running_vm = self.run_step(
                "Start the VM and reset Talon",
                self.vm_controller.start,
                success_message="VM started and Talon reset.",
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
                lambda: self.vm_controller.restart_talon(
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
                "Trigger a visible guest dialog",
                lambda: self.trigger_visual_change(running_vm, token),
                success_message="Guest dialog triggered.",
            )
            self.run_step(
                "Capture a second screenshot after the guest dialog",
                lambda: talon_client.capture_screenshot(screenshot_path),
                success_message="Second screenshot captured.",
            )
            self.run_step(
                "Validate the second screenshot artifact",
                lambda: self.validate_screenshot(screenshot_path),
                success_message="Second screenshot artifact validated.",
            )
            self.run_step(
                "Verify the screenshots changed after the guest dialog",
                lambda: self.verify_screenshots_differ(
                    baseline_screenshot_path, screenshot_path
                ),
                success_message="Screenshots changed after the guest dialog.",
            )
        except click.ClickException as error:
            self._fail(str(error), screenshot_path=hint_screenshot())
        except click.exceptions.Exit:
            raise
        finally:
            self._hint_screenshot = None
            if started:
                self.run_step(
                    "Stop the VM after smoke-test",
                    self.vm_controller.stop,
                    success_message="VM stopped after smoke-test.",
                )

        self.log("PASS", "Smoke test completed successfully.")

    def write_bundle(self, bundle_dir: Path, marker_path: str, token: str) -> None:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "talonbox_smoke_test.talon").write_text(
            "-\ntalonbox smoke test:\n    user.talonbox_smoke_test()\n",
            encoding="utf-8",
        )
        (bundle_dir / "talonbox_smoke_test.py").write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "",
                    "from talon import Module",
                    "",
                    "mod = Module()",
                    "",
                    "@mod.action_class",
                    "class Actions:",
                    "    def talonbox_smoke_test() -> None:",
                    '        """Write the talonbox smoke-test marker file."""',
                    f"        Path({marker_path!r}).write_text({token!r}, encoding='utf-8')",
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
                "-av",
                f"{bundle_dir}/",
                "guest:/Users/lume/.talon/user/talonbox_smoke_test/",
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

    def trigger_visual_change(self, running_vm: RunningVm, token: str) -> None:
        dialog_log = f"/tmp/talonbox-smoke-test-dialog-{token}.log"
        script = (
            f'display dialog "talonbox screenshot test {token}" '
            'buttons {"OK"} default button 1 giving up after 15'
        )
        running_vm.run_shell(
            (
                f"nohup osascript -e {shlex.quote(script)} "
                f">{shlex.quote(dialog_log)} 2>&1 & sleep 1"
            ),
        )

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
        if screenshot_path is not None:
            click.echo(f"HINT inspect the saved screenshot at {screenshot_path}.")
