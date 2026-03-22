# talonbox Code Walkthrough

*2026-03-22T06:21:49Z by Showboat 0.6.1*
<!-- showboat-id: b6233d92-7842-47e2-aea7-a06daa5b4cf3 -->

This walkthrough follows the same path a real `talonbox` invocation takes through the codebase. It starts at packaging and entrypoints, moves through the Click command tree, drops into VM lifecycle and SSH transport, explains the transfer sandbox and Talon RPC helpers, and ends with the smoke test and the test suite that lock the behavior in place.

The repository is small enough that a linear read works well, but the modules are layered carefully: `cli.py` exposes user-facing commands, `VmController` manages lifecycle decisions, `RunningVm` handles SSH and Talon transport, `TransferService` enforces host write restrictions, `TalonClient` wraps Talon-native operations, and `SmokeTestRunner` composes those building blocks into a full end-to-end diagnostic.

## 1. Packaging and Entry Points

The project definition in `pyproject.toml` says a lot about intent before we ever open the implementation. `talonbox` is a very small CLI package with Click as its only runtime dependency. The console script points directly at `talonbox.cli:cli`, which means almost all user-visible behavior fans out from that one module.

```bash
nl -ba pyproject.toml | sed -n '1,80p'
```

```output
     1	[project]
     2	name = "talonbox"
     3	version = "0.1.0"
     4	description = "A local sandbox for testing Talon scripts. Inspired by playwright-cli."
     5	readme = "README.md"
     6	authors = [{name = "James Stout"}]
     7	requires-python = ">=3.11"
     8	classifiers = [
     9	    "License :: OSI Approved :: Apache Software License"
    10	]
    11	dependencies = [
    12	    "click"
    13	]
    14	
    15	[build-system]
    16	requires = ["hatchling"]
    17	build-backend = "hatchling.build"
    18	
    19	[project.urls]
    20	Homepage = "https://github.com/wolfmanstout/talonbox"
    21	Changelog = "https://github.com/wolfmanstout/talonbox/releases"
    22	Issues = "https://github.com/wolfmanstout/talonbox/issues"
    23	CI = "https://github.com/wolfmanstout/talonbox/actions"
    24	
    25	[project.scripts]
    26	talonbox = "talonbox.cli:cli"
    27	
    28	[dependency-groups]
    29	dev = [
    30	    "pytest",
    31	]
    32	
    33	[tool.ruff.lint]
    34	select = [
    35	    # pycodestyle
    36	    "E",
    37	    # Pyflakes
    38	    "F",
    39	    # pyupgrade
    40	    "UP",
    41	    # flake8-bugbear
    42	    "B",
    43	    # flake8-simplify
    44	    "SIM",
    45	    # isort
    46	    "I",
    47	]
    48	ignore = ["E501", "SIM105", "SIM116", "UP045"]
    49	
    50	[tool.pyright]
    51	venvPath = "."
    52	venv = ".venv"
    53	pythonVersion = "3.11"
```

A few details are worth calling out here. The short description matches the repo norm in `AGENTS.md`. The runtime dependency list is intentionally minimal, which makes the rest of the codebase easier to understand because most of the behavior is implemented directly in the repository rather than outsourced to helper libraries. The `project.scripts` entry is also the key bridge between installation and execution: once installed, `talonbox` is just the `cli()` Click group from `src/talonbox/cli.py`.

```bash
nl -ba src/talonbox/__main__.py
```

```output
     1	from .cli import cli
     2	
     3	if __name__ == "__main__":
     4	    cli()
```

`python -m talonbox` is almost a thin alias: `__main__.py` imports the Click group and invokes it. That keeps the package entrypoints consistent whether the tool is launched through the console script or through Python module execution.

## 2. The CLI Module Defines the User-Facing Shape

`src/talonbox/cli.py` is the public front door. It does three big jobs: it defines the root Click group and help text, it stores global settings like the VM name and debug flag, and it wires each subcommand to the lower-level service object that actually performs the work.

```bash
nl -ba src/talonbox/cli.py | sed -n '1,140p'
```

```output
     1	from __future__ import annotations
     2	
     3	import sys
     4	from dataclasses import dataclass
     5	from pathlib import Path
     6	
     7	import click
     8	
     9	from .smoke_test import SmokeTestRunner
    10	from .talon_client import TalonClient
    11	from .transfer import TransferService
    12	from .vm import VmController
    13	
    14	DEFAULT_VM = "talon-test"
    15	HELP_COMMAND_GROUPS = (
    16	    ("VM lifecycle", ("setup", "start", "restart-talon", "smoke-test", "stop", "show")),
    17	    ("Guest shell", ("exec", "rsync", "scp")),
    18	    ("Talon RPC", ("repl", "mimic", "screenshot")),
    19	)
    20	
    21	
    22	def _examples_epilog(*examples: str) -> str:
    23	    body = "\n".join(f"  {example}" for example in examples)
    24	    return f"\b\nExamples:\n{body}"
    25	
    26	
    27	class TalonboxGroup(click.Group):
    28	    def format_commands(
    29	        self, ctx: click.Context, formatter: click.HelpFormatter
    30	    ) -> None:
    31	        emitted: set[str] = set()
    32	        for title, command_names in HELP_COMMAND_GROUPS:
    33	            rows: list[tuple[str, str]] = []
    34	            for command_name in command_names:
    35	                cmd = self.get_command(ctx, command_name)
    36	                if cmd is None or cmd.hidden:
    37	                    continue
    38	                rows.append((command_name, cmd.get_short_help_str()))
    39	                emitted.add(command_name)
    40	            if rows:
    41	                with formatter.section(title):
    42	                    formatter.write_dl(rows)
    43	
    44	        remaining_rows: list[tuple[str, str]] = []
    45	        for command_name in self.list_commands(ctx):
    46	            if command_name in emitted:
    47	                continue
    48	            cmd = self.get_command(ctx, command_name)
    49	            if cmd is None or cmd.hidden:
    50	                continue
    51	            remaining_rows.append((command_name, cmd.get_short_help_str()))
    52	        if remaining_rows:
    53	            with formatter.section("Other"):
    54	                formatter.write_dl(remaining_rows)
    55	
    56	
    57	@dataclass(slots=True)
    58	class CliSettings:
    59	    vm: str
    60	    debug: bool
    61	
    62	
    63	pass_settings = click.make_pass_decorator(CliSettings)
    64	
    65	
    66	def _require_macos() -> None:
    67	    if sys.platform != "darwin":
    68	        raise click.ClickException("talonbox currently supports only macOS hosts.")
    69	
    70	
    71	def _echo_vm_info(vm_controller: VmController, info: object) -> None:
    72	    assert hasattr(info, "status")
    73	    for line in vm_controller.format_vm_info(info):  # type: ignore[arg-type]
    74	        click.echo(line)
    75	
    76	
    77	def _build_talon_client(settings: CliSettings) -> TalonClient:
    78	    vm_controller = VmController(settings.vm, settings.debug)
    79	    running_vm = vm_controller.get_running_vm()
    80	    transfer_service = TransferService(running_vm)
    81	    return TalonClient(running_vm, transfer_service)
    82	
    83	
    84	def _build_smoke_test_runner(settings: CliSettings) -> SmokeTestRunner:
    85	    vm_controller = VmController(settings.vm, settings.debug)
    86	    return SmokeTestRunner(vm_controller)
    87	
    88	
    89	@click.group(
    90	    name="talonbox",
    91	    cls=TalonboxGroup,
    92	    context_settings={"max_content_width": 100},
    93	    help=(
    94	        "Minimal Talon VM control primitives for coding agents.\n\n"
    95	        "Use `start` to boot the VM and reset Talon to a clean state. Use `exec` and `rsync` "
    96	        "for general guest access. Use `repl`, `mimic`, and `screenshot` for predictable "
    97	        "Talon-native operations. Use `show` for a read-only status check; it does not modify "
    98	        "the VM."
    99	    ),
   100	    epilog=_examples_epilog(
   101	        "talonbox start",
   102	        "talonbox smoke-test",
   103	        "talonbox restart-talon",
   104	        "talonbox exec -- uname -a",
   105	        "talonbox rsync -av ~/.talon/user/ guest:/Users/lume/.talon/user/",
   106	        "talonbox scp guest:/tmp/out.png /tmp/out.png",
   107	        "talonbox mimic 'focus chrome'",
   108	        "talonbox screenshot /tmp/talon.png",
   109	    ),
   110	)
   111	@click.option("--vm", default=DEFAULT_VM, show_default=True, help="Target VM name.")
   112	@click.option(
   113	    "--debug",
   114	    is_flag=True,
   115	    envvar="TALONBOX_DEBUG",
   116	    help="Print invoked commands and failure details to stderr. Can also be enabled with TALONBOX_DEBUG=1.",
   117	)
   118	@click.version_option(prog_name="talonbox")
   119	@click.pass_context
   120	def cli(click_ctx: click.Context, vm: str, debug: bool) -> None:
   121	    _require_macos()
   122	    click_ctx.obj = CliSettings(vm=vm, debug=debug)
   123	
   124	
   125	@cli.command(
   126	    short_help="Create or provision the test VM (stub for now).",
   127	    help="Create or provision the Talon test VM.\n\nThis command is reserved for future setup automation.",
   128	    epilog=_examples_epilog("talonbox setup"),
   129	)
   130	def setup() -> None:
   131	    raise click.ClickException("setup is not implemented yet")
   132	
   133	
   134	@cli.command(
   135	    short_help="Boot the VM, wipe the Talon user dir, and restart Talon.",
   136	    help=(
   137	        "Start the VM in the background, wait for SSH, clear the guest Talon user directory, "
   138	        "and relaunch Talon under Rosetta.\n\n"
   139	        "Talon is launched through Terminal so guest Screen Recording permissions apply to "
   140	        "the process that captures screenshots.\n\n"
```

The top of the file establishes the command taxonomy. `HELP_COMMAND_GROUPS` is a presentation-only structure that lets `TalonboxGroup.format_commands()` render help in meaningful sections instead of a single alphabetical blob. That is a small implementation detail, but it tells you a lot about the tool: the author expects humans and coding agents to browse this command list regularly, so the commands are grouped by workflow rather than by accident of naming.

`CliSettings` is the other important piece in this first chunk. Click stores an instance of it on the context so every subcommand can get the selected VM name and debug flag without re-parsing options. The small helper constructors such as `_build_talon_client()` and `_build_smoke_test_runner()` show the dependency graph clearly: both helpers build a `VmController`, and the Talon path additionally resolves a running VM and wraps it in `TransferService` and `TalonClient`.

```bash
nl -ba src/talonbox/cli.py | sed -n '140,260p'
```

```output
   140	        "the process that captures screenshots.\n\n"
   141	        "The command fails if the VM is already running."
   142	    ),
   143	    epilog=_examples_epilog(
   144	        "talonbox start",
   145	        "talonbox --vm talon-test --debug start",
   146	    ),
   147	)
   148	@pass_settings
   149	def start(settings: CliSettings) -> None:
   150	    vm_controller = VmController(settings.vm, settings.debug)
   151	    _echo_vm_info(vm_controller, vm_controller.start().to_vm_info())
   152	
   153	
   154	@cli.command(
   155	    short_help="Restart Talon inside the running VM and reset Talon logs.",
   156	    help=(
   157	        "Restart Talon inside the running VM without rebooting the VM.\n\n"
   158	        "This truncates `~/.talon/talon.log` and `/tmp/talonbox-talon.log`, then relaunches "
   159	        "Talon under Rosetta through Terminal so screen capture permissions still apply."
   160	    ),
   161	    epilog=_examples_epilog(
   162	        "talonbox restart-talon",
   163	        "talonbox --debug restart-talon",
   164	    ),
   165	)
   166	@pass_settings
   167	def restart_talon(settings: CliSettings) -> None:
   168	    VmController(settings.vm, settings.debug).restart_talon(
   169	        wipe_user_dir=False,
   170	        clean_logs=True,
   171	    )
   172	
   173	
   174	@cli.command(
   175	    short_help="Stop the VM if it is running.",
   176	    help=(
   177	        "Log out the guest GUI session when possible, then stop the VM. Safe to run repeatedly."
   178	    ),
   179	    epilog=_examples_epilog(
   180	        "talonbox stop",
   181	        "talonbox --vm talon-test stop",
   182	    ),
   183	)
   184	@pass_settings
   185	def stop(settings: CliSettings) -> None:
   186	    VmController(settings.vm, settings.debug).stop()
   187	
   188	
   189	@cli.command(
   190	    name="smoke-test",
   191	    short_help="Run a basic end-to-end diagnostic against the Talon VM.",
   192	    help=(
   193	        "Run a mutating end-to-end sanity check for talonbox.\n\n"
   194	        "This command may stop a running VM, starts the VM cleanly, uploads a temporary Talon "
   195	        "command bundle, runs mimic(), verifies a guest-side marker file, captures a screenshot, "
   196	        "and stops the VM again.\n\n"
   197	        "Artifacts are kept under `/tmp` for debugging, and the VM is left stopped after the run."
   198	    ),
   199	    epilog=_examples_epilog(
   200	        "talonbox smoke-test",
   201	        "talonbox --debug smoke-test",
   202	        "talonbox smoke-test --yes",
   203	    ),
   204	)
   205	@click.option(
   206	    "-y",
   207	    "--yes",
   208	    is_flag=True,
   209	    help="Skip the confirmation prompt if the VM is already running.",
   210	)
   211	@pass_settings
   212	def smoke_test(settings: CliSettings, yes: bool) -> None:
   213	    _build_smoke_test_runner(settings).run(yes=yes)
   214	
   215	
   216	@cli.command(
   217	    short_help="Print VM status and connection details without changing anything.",
   218	    help=(
   219	        "Show whether the VM is running. When it is running, also print IP, SSH credentials, "
   220	        "and the VNC link.\n\n"
   221	        "This command is read-only: it does not start, stop, or modify the VM, and is safe to "
   222	        "use in sandboxed environments that permit running `lume ls`."
   223	    ),
   224	    epilog=_examples_epilog(
   225	        "talonbox show",
   226	        "talonbox --vm talon-test show",
   227	    ),
   228	)
   229	@pass_settings
   230	def show(settings: CliSettings) -> None:
   231	    vm_controller = VmController(settings.vm, settings.debug)
   232	    _echo_vm_info(vm_controller, vm_controller.get_vm())
   233	
   234	
   235	@cli.command(
   236	    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
   237	    short_help="Run a command on the guest via SSH.",
   238	    help=(
   239	        "Run a command on the guest VM over SSH.\n\n"
   240	        "Place `--` before the remote command so talonbox stops parsing options.\n\n"
   241	        "For shell pipelines or redirects, pass a single quoted shell string."
   242	    ),
   243	    epilog=_examples_epilog(
   244	        "talonbox exec -- whoami",
   245	        "talonbox exec -- sh -lc 'ls -la ~/.talon'",
   246	        'talonbox exec -- "ps aux | grep Safari"',
   247	    ),
   248	)
   249	@click.argument("command", nargs=-1, type=click.UNPROCESSED, metavar="COMMAND...")
   250	@pass_settings
   251	def exec_command(settings: CliSettings, command: tuple[str, ...]) -> None:
   252	    if not command:
   253	        raise click.ClickException("No command provided")
   254	    result = (
   255	        VmController(settings.vm, settings.debug)
   256	        .get_running_vm()
   257	        .run_shell(
   258	            command[0] if len(command) == 1 else list(command),
   259	            stream=True,
   260	            check=False,
```

The middle of `cli.py` covers the lifecycle-oriented commands. `start` is intentionally opinionated: it does not merely boot a VM, it boots the VM and resets Talon into a clean state. `restart-talon` narrows the scope to Talon itself. `stop` is idempotent. `show` is explicitly read-only. `smoke-test` is the strongest statement of intended workflow because it advertises itself as a mutating end-to-end diagnostic and delegates the whole sequence to `SmokeTestRunner` rather than inlining a wall of logic in the CLI layer.

```bash
nl -ba src/talonbox/cli.py | sed -n '260,420p'
```

```output
   260	            check=False,
   261	        )
   262	    )
   263	    if result.returncode:
   264	        raise click.exceptions.Exit(result.returncode)
   265	
   266	
   267	@cli.command(
   268	    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
   269	    short_help="Copy files between host and guest with rsync.",
   270	    help=(
   271	        "Run rsync between the host and the guest VM.\n\n"
   272	        "Use explicit `guest:/path` operands for the VM side. Exactly one side may be remote, "
   273	        "and only `guest:` remote paths are allowed. No other remotes are permitted.\n\n"
   274	        "Local sources may be read from anywhere, but any host-side output must stay under "
   275	        "`/tmp`. Transfers run inside the macOS sandbox, so extra host-side writes outside "
   276	        "that boundary fail with an obvious permission error."
   277	    ),
   278	    epilog=_examples_epilog(
   279	        "talonbox rsync -av ./repo/ guest:/Users/lume/.talon/user/repo/",
   280	        "talonbox rsync -av guest:/Users/lume/Pictures/ /tmp/guest-pictures/",
   281	    ),
   282	)
   283	@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="RSYNC_ARGS...")
   284	@pass_settings
   285	def rsync(settings: CliSettings, args: tuple[str, ...]) -> None:
   286	    running_vm = VmController(settings.vm, settings.debug).get_running_vm()
   287	    returncode = TransferService(running_vm).rsync(args)
   288	    if returncode:
   289	        raise click.exceptions.Exit(returncode)
   290	
   291	
   292	@cli.command(
   293	    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
   294	    short_help="Copy files between host and guest with scp.",
   295	    help=(
   296	        "Run scp between the host and the guest VM.\n\n"
   297	        "Use explicit `guest:/path` operands for the VM side. Exactly one side may be remote, "
   298	        "and only `guest:` remote paths are allowed. No other remotes are permitted.\n\n"
   299	        "Local sources may be read from anywhere, but any host-side output must stay under "
   300	        "`/tmp`. Transfers run inside the macOS sandbox, so extra host-side writes outside "
   301	        "that boundary fail with an obvious permission error."
   302	    ),
   303	    epilog=_examples_epilog(
   304	        "talonbox scp ./settings.talon guest:/Users/lume/.talon/user/settings.talon",
   305	        "talonbox scp guest:/tmp/out.png /tmp/out.png",
   306	    ),
   307	)
   308	@click.argument("args", nargs=-1, type=click.UNPROCESSED, metavar="SCP_ARGS...")
   309	@pass_settings
   310	def scp(settings: CliSettings, args: tuple[str, ...]) -> None:
   311	    running_vm = VmController(settings.vm, settings.debug).get_running_vm()
   312	    returncode = TransferService(running_vm).scp(args)
   313	    if returncode:
   314	        raise click.exceptions.Exit(returncode)
   315	
   316	
   317	@cli.command(
   318	    short_help="Pipe Python into the guest Talon REPL.",
   319	    help=(
   320	        "Send Python to the guest Talon REPL.\n\n"
   321	        "Provide CODE as an argument or pipe Python on stdin. This command is intentionally "
   322	        "non-interactive."
   323	    ),
   324	    epilog=_examples_epilog(
   325	        "talonbox repl 'print(1+1)'",
   326	        "printf 'print(1+1)\\n' | talonbox repl",
   327	    ),
   328	)
   329	@click.argument("code", required=False, metavar="[CODE]")
   330	@pass_settings
   331	def repl(settings: CliSettings, code: str | None) -> None:
   332	    if code is None:
   333	        if sys.stdin.isatty():
   334	            raise click.ClickException(
   335	                "No code provided. Pass CODE or pipe Python into stdin."
   336	            )
   337	        code = sys.stdin.read()
   338	    assert code is not None
   339	    _build_talon_client(settings).repl(code)
   340	
   341	
   342	@cli.command(
   343	    short_help="Run a voice command through Talon's mimic().",
   344	    help="Send one phrase to the guest Talon REPL as `mimic(<phrase>)`.",
   345	    epilog=_examples_epilog(
   346	        "talonbox mimic 'focus chrome'",
   347	        "talonbox mimic 'tab close'",
   348	    ),
   349	)
   350	@click.argument("command", metavar="PHRASE")
   351	@pass_settings
   352	def mimic(settings: CliSettings, command: str) -> None:
   353	    _build_talon_client(settings).mimic(command)
   354	
   355	
   356	@cli.command(
   357	    short_help="Capture a screenshot in the guest and download it locally.",
   358	    help=(
   359	        "Use Talon's screen capture API inside the guest, save the image to a guest temp file, "
   360	        "download it to a host path under `/tmp`, and remove the guest temp file."
   361	    ),
   362	    epilog=_examples_epilog(
   363	        "talonbox screenshot /tmp/talon.png",
   364	        "talonbox --vm talon-test screenshot /tmp/guest-screen.png",
   365	    ),
   366	)
   367	@click.argument(
   368	    "filepath", metavar="HOST_PATH", type=click.Path(dir_okay=False, path_type=Path)
   369	)
   370	@pass_settings
   371	def screenshot(settings: CliSettings, filepath: Path) -> None:
   372	    _build_talon_client(settings).capture_screenshot(filepath)
   373	
   374	
   375	def main() -> int:
   376	    cli.main(standalone_mode=False)
   377	    return 0
   378	
   379	
   380	if __name__ == "__main__":
   381	    sys.exit(main())
```

The bottom of the file handles the command families that touch a running guest. `exec`, `rsync`, and `scp` all resolve a running VM and then delegate immediately. `repl`, `mimic`, and `screenshot` go through `TalonClient`, which is a helpful design choice because it keeps Talon-specific behavior out of the generic CLI plumbing.

Two subtle details matter here. First, `exec` uses `ignore_unknown_options` and `allow_interspersed_args=False` so the remote shell command can be passed through without Click trying to interpret it. Second, `repl` intentionally supports either an argument or stdin, but remains non-interactive by design. That keeps the tool deterministic for agents and scripts.

## 3. Lume Is the Lowest-Level VM Adapter

The next layer down is `src/talonbox/lume.py`. This module does not know anything about Talon commands or transfer policy. Its job is narrower: talk to the external `lume` CLI, normalize its output into Python dataclasses, and provide a few waiting and cleanup helpers around VM startup and shutdown.

```bash
nl -ba src/talonbox/lume.py | sed -n '1,220p'
```

```output
     1	from __future__ import annotations
     2	
     3	import json
     4	import os
     5	import signal
     6	import subprocess
     7	import sys
     8	import tempfile
     9	import time
    10	from collections.abc import Mapping
    11	from dataclasses import dataclass
    12	from pathlib import Path
    13	from typing import Any
    14	
    15	
    16	class LumeError(RuntimeError):
    17	    pass
    18	
    19	
    20	@dataclass(slots=True)
    21	class VmInfo:
    22	    name: str
    23	    status: str
    24	    ip_address: str | None
    25	    vnc_url: str | None = None
    26	
    27	
    28	@dataclass(slots=True)
    29	class VmLaunch:
    30	    process: subprocess.Popen[bytes]
    31	    log_path: Path
    32	
    33	
    34	def _debug_log(debug: bool, message: str) -> None:
    35	    if debug:
    36	        print(message, file=sys.stderr)
    37	
    38	
    39	def _run_lume(
    40	    args: list[str],
    41	    *,
    42	    debug: bool = False,
    43	    capture_output: bool = True,
    44	) -> subprocess.CompletedProcess[str]:
    45	    cmd = ["lume", *args]
    46	    if debug:
    47	        _debug_log(debug, f"+ {' '.join(cmd)}")
    48	    result = subprocess.run(
    49	        cmd,
    50	        check=False,
    51	        text=True,
    52	        capture_output=capture_output,
    53	    )
    54	    if result.returncode != 0:
    55	        message = (
    56	            result.stderr.strip() or result.stdout.strip() or "lume command failed"
    57	        )
    58	        raise LumeError(message)
    59	    return result
    60	
    61	
    62	def get_vm_info(name: str, *, debug: bool = False) -> VmInfo | None:
    63	    result = _run_lume(["ls", "--format", "json"], debug=debug)
    64	    try:
    65	        records = _parse_lume_json(result.stdout)
    66	    except json.JSONDecodeError as error:
    67	        raw_output = result.stdout.strip() or "<empty stdout>"
    68	        raise LumeError(
    69	            f"Invalid JSON from `lume ls --format json`: {raw_output}"
    70	        ) from error
    71	    for record in records:
    72	        if record.get("name") == name:
    73	            return VmInfo(
    74	                name=name,
    75	                status=record.get("status", "unknown"),
    76	                ip_address=record.get("ipAddress"),
    77	                vnc_url=record.get("vncUrl"),
    78	            )
    79	    return None
    80	
    81	
    82	def wait_for_status(
    83	    name: str,
    84	    expected_status: str,
    85	    *,
    86	    timeout: float,
    87	    interval: float = 2.0,
    88	    debug: bool = False,
    89	) -> VmInfo:
    90	    deadline = time.monotonic() + timeout
    91	    while True:
    92	        info = get_vm_info(name, debug=debug)
    93	        if info is None:
    94	            raise LumeError(f"VM not found: {name}")
    95	        if info.status == expected_status:
    96	            return info
    97	        if time.monotonic() >= deadline:
    98	            raise LumeError(
    99	                f"Timed out waiting for VM to reach status {expected_status}: {name}"
   100	            )
   101	        time.sleep(interval)
   102	
   103	
   104	def wait_for_running_vm(
   105	    name: str,
   106	    *,
   107	    timeout: float,
   108	    interval: float = 2.0,
   109	    debug: bool = False,
   110	    launch: VmLaunch | None = None,
   111	) -> VmInfo:
   112	    deadline = time.monotonic() + timeout
   113	    while True:
   114	        info = get_vm_info(name, debug=debug)
   115	        if info is None:
   116	            raise LumeError(f"VM not found: {name}")
   117	        if info.status == "running" and info.ip_address:
   118	            return info
   119	        if launch is not None:
   120	            returncode = launch.process.poll()
   121	            if returncode is not None:
   122	                raise LumeError(
   123	                    _format_launch_failure(
   124	                        launch.log_path,
   125	                        f"lume run exited before VM became ready: {name} (exit code {returncode})",
   126	                    )
   127	                )
   128	        if time.monotonic() >= deadline:
   129	            detail = (
   130	                _format_launch_failure(
   131	                    launch.log_path,
   132	                    f"Timed out waiting for VM to start: {name}",
   133	                )
   134	                if launch is not None
   135	                else f"Timed out waiting for VM to start: {name}"
   136	            )
   137	            raise LumeError(detail)
   138	        time.sleep(interval)
   139	
   140	
   141	def spawn_vm(name: str, *, debug: bool = False) -> VmLaunch:
   142	    cmd = ["lume", "run", name, "--no-display"]
   143	    if debug:
   144	        _debug_log(debug, f"+ {' '.join(cmd)}")
   145	    with tempfile.NamedTemporaryFile(
   146	        mode="w+b",
   147	        delete=False,
   148	        prefix="talonbox-lume-run-",
   149	        suffix=".log",
   150	        dir="/tmp",
   151	    ) as log_file:
   152	        process = subprocess.Popen(
   153	            cmd,
   154	            stdout=log_file,
   155	            stderr=subprocess.STDOUT,
   156	            start_new_session=True,
   157	        )
   158	        return VmLaunch(process=process, log_path=Path(log_file.name))
   159	
   160	
   161	def stop_vm(name: str, *, debug: bool = False) -> None:
   162	    _run_lume(["stop", name], debug=debug)
   163	
   164	
   165	def force_stop_vm(name: str, *, debug: bool = False) -> None:
   166	    pgids = _collect_vm_process_groups(name, debug=debug)
   167	    if not pgids:
   168	        raise LumeError(f"Unable to find local Lume process for VM: {name}")
   169	
   170	    for pgid in pgids:
   171	        _kill_process_group(pgid, signal.SIGTERM, debug=debug)
   172	    time.sleep(2.0)
   173	
   174	    remaining = {pgid for pgid in pgids if _process_group_exists(pgid)}
   175	    for pgid in remaining:
   176	        _kill_process_group(pgid, signal.SIGKILL, debug=debug)
   177	
   178	
   179	def cleanup_launch_log(log_path: Path) -> None:
   180	    try:
   181	        log_path.unlink()
   182	    except FileNotFoundError:
   183	        return
   184	
   185	
   186	def _format_launch_failure(log_path: Path, summary: str) -> str:
   187	    detail = _read_launch_log(log_path)
   188	    if not detail:
   189	        return summary
   190	    return f"{summary}\n{detail}\nstartup log: {log_path}"
   191	
   192	
   193	def _read_launch_log(log_path: Path, *, max_lines: int = 20) -> str:
   194	    try:
   195	        text = log_path.read_text(encoding="utf-8", errors="replace")
   196	    except FileNotFoundError:
   197	        return ""
   198	
   199	    lines = [line for line in text.splitlines() if line.strip()]
   200	    if not lines:
   201	        return ""
   202	    return "\n".join(lines[-max_lines:])
   203	
   204	
   205	def _parse_lume_json(output: str) -> list[dict[str, Any]]:
   206	    try:
   207	        parsed = json.loads(output)
   208	    except json.JSONDecodeError:
   209	        lines = output.splitlines()
   210	        for index, line in enumerate(lines):
   211	            stripped = line.lstrip()
   212	            if stripped == "[" or stripped.startswith("[{") or stripped.startswith("{"):
   213	                parsed = json.loads("\n".join(lines[index:]))
   214	                break
   215	        else:
   216	            raise
   217	
   218	    if not isinstance(parsed, list):
   219	        raise json.JSONDecodeError("Expected a JSON list", output, 0)
   220	
```

There are three ideas to keep in mind while reading this file. First, `VmInfo` and `VmLaunch` convert shell-oriented state into explicit Python objects, which makes the rest of the code much easier to test. Second, `_run_lume()` is intentionally strict: any non-zero exit becomes `LumeError`, so callers do not have to duplicate error handling every time they touch `lume`. Third, the waiting helpers are polling loops with timeouts rather than event-driven hooks, which keeps the implementation portable and straightforward.

`get_vm_info()` is especially careful. It expects `lume ls --format json`, but `_parse_lume_json()` tolerates noisy log lines before the JSON payload. That defensive parser matters because tools that shell out to external CLIs often fail in messy ways when stderr/stdout formatting changes. Here, the parser does a little extra work so the higher layers can remain simple.

```bash
nl -ba src/talonbox/lume.py | sed -n '220,320p'
```

```output
   220	
   221	    records: list[dict[str, Any]] = []
   222	    for record in parsed:
   223	        if not isinstance(record, Mapping):
   224	            raise json.JSONDecodeError("Expected JSON objects in list", output, 0)
   225	        records.append(dict(record))
   226	    return records
   227	
   228	
   229	def _collect_vm_process_groups(name: str, *, debug: bool) -> set[int]:
   230	    pgids: set[int] = set()
   231	
   232	    for process in _list_processes(debug=debug):
   233	        if f"lume run {name}" not in process.command:
   234	            continue
   235	        pgids.add(process.pgid)
   236	    return {pgid for pgid in pgids if pgid > 1}
   237	
   238	
   239	@dataclass(slots=True)
   240	class _ProcessInfo:
   241	    pid: int
   242	    pgid: int
   243	    command: str
   244	
   245	
   246	def _list_processes(*, debug: bool) -> list[_ProcessInfo]:
   247	    result = subprocess.run(
   248	        ["ps", "-Ao", "pid=,pgid=,command="],
   249	        check=False,
   250	        text=True,
   251	        capture_output=True,
   252	    )
   253	    if result.returncode != 0:
   254	        message = result.stderr.strip() or result.stdout.strip() or "ps command failed"
   255	        raise LumeError(message)
   256	
   257	    processes: list[_ProcessInfo] = []
   258	    for line in result.stdout.splitlines():
   259	        raw = line.strip()
   260	        if not raw:
   261	            continue
   262	        parts = raw.split(None, 2)
   263	        if len(parts) != 3:
   264	            continue
   265	        try:
   266	            pid = int(parts[0])
   267	            pgid = int(parts[1])
   268	        except ValueError:
   269	            continue
   270	        processes.append(_ProcessInfo(pid=pid, pgid=pgid, command=parts[2]))
   271	    if debug:
   272	        _debug_log(
   273	            debug,
   274	            f"found {len(processes)} local processes while scanning for stuck VMs",
   275	        )
   276	    return processes
   277	
   278	
   279	def _kill_process_group(pgid: int, sig: signal.Signals, *, debug: bool) -> None:
   280	    if debug:
   281	        _debug_log(debug, f"+ kill -{sig.name} -- -{pgid}")
   282	    try:
   283	        os.killpg(pgid, sig)
   284	    except ProcessLookupError:
   285	        return
   286	
   287	
   288	def _process_group_exists(pgid: int) -> bool:
   289	    try:
   290	        os.killpg(pgid, 0)
   291	    except ProcessLookupError:
   292	        return False
   293	    except PermissionError:
   294	        return True
   295	    else:
   296	        return True
```

The bottom of the file explains the force-stop fallback. If graceful `lume stop` is not enough, `force_stop_vm()` scans the local process table for `lume run <name>`, collects process groups, sends `SIGTERM`, waits a moment, and then escalates to `SIGKILL` for any still-running groups. That fallback is host-centric and pragmatic: if the VM launcher wedges, `talonbox` still wants a deterministic way to get back to a stopped state.

## 4. `vm.py` Turns VM State into a Running Session

If `lume.py` is the raw adapter, `src/talonbox/vm.py` is the first real orchestration layer. It introduces two central abstractions. `RunningVm` represents a specific running guest that can answer SSH, and `VmController` decides when and how to move a named VM between stopped and running states.

```bash
nl -ba src/talonbox/vm.py | sed -n '1,220p'
```

```output
     1	from __future__ import annotations
     2	
     3	import shlex
     4	import subprocess
     5	import sys
     6	import time
     7	from pathlib import Path
     8	
     9	import click
    10	
    11	from . import lume
    12	
    13	TALON_BINARY = "/Applications/Talon.app/Contents/MacOS/Talon"
    14	TALON_LOG = "$HOME/.talon/talon.log"
    15	START_TIMEOUT_SECONDS = 180.0
    16	SSH_TIMEOUT_SECONDS = 60.0
    17	TALON_TIMEOUT_SECONDS = 30.0
    18	TALON_REPL_TIMEOUT_SECONDS = 30.0
    19	TALON_POST_RESTART_SETTLE_SECONDS = 3.0
    20	TRANSIENT_RETRY_DELAY_SECONDS = 1.0
    21	TRANSIENT_RETRY_ATTEMPTS = 2
    22	
    23	
    24	class TransportError(RuntimeError):
    25	    pass
    26	
    27	
    28	class RemoteCommandError(TransportError):
    29	    pass
    30	
    31	
    32	class RunningVm:
    33	    SSH_USERNAME = "lume"
    34	    SSH_PASSWORD = "lume"
    35	    SSH_OPTIONS = [
    36	        "-o",
    37	        "StrictHostKeyChecking=no",
    38	        "-o",
    39	        "UserKnownHostsFile=/dev/null",
    40	        "-o",
    41	        "LogLevel=ERROR",
    42	        "-o",
    43	        "BatchMode=no",
    44	        "-o",
    45	        "NumberOfPasswordPrompts=1",
    46	        "-o",
    47	        "PasswordAuthentication=yes",
    48	        "-o",
    49	        "KbdInteractiveAuthentication=no",
    50	        "-o",
    51	        "PreferredAuthentications=password",
    52	        "-o",
    53	        "PubkeyAuthentication=no",
    54	    ]
    55	
    56	    def __init__(
    57	        self,
    58	        *,
    59	        name: str,
    60	        ip_address: str,
    61	        debug: bool,
    62	        vnc_url: str | None = None,
    63	    ) -> None:
    64	        self.name = name
    65	        self.ip_address = ip_address
    66	        self.debug = debug
    67	        self.vnc_url = vnc_url
    68	
    69	    def to_vm_info(self) -> lume.VmInfo:
    70	        return lume.VmInfo(
    71	            name=self.name,
    72	            status="running",
    73	            ip_address=self.ip_address,
    74	            vnc_url=self.vnc_url,
    75	        )
    76	
    77	    def run_shell(
    78	        self,
    79	        command: str | list[str],
    80	        *,
    81	        timeout: float | None = None,
    82	        poll: bool = False,
    83	        stream: bool = False,
    84	        check: bool = True,
    85	    ) -> subprocess.CompletedProcess[str]:
    86	        remote_command = command if isinstance(command, str) else shlex.join(command)
    87	        result = self._run_transport_command(
    88	            [*self._ssh_command_prefix(), f"sh -lc {shlex.quote(remote_command)}"],
    89	            timeout=timeout,
    90	            poll=poll,
    91	            stream=stream,
    92	        )
    93	        if check and result.returncode != 0:
    94	            message = result.stderr.strip() if result.stderr else ""
    95	            if not message and result.stdout:
    96	                message = result.stdout.strip()
    97	            raise RemoteCommandError(
    98	                message or f"Remote command failed: {remote_command}"
    99	            )
   100	        return result
   101	
   102	    def run_repl(
   103	        self,
   104	        payload: str,
   105	        *,
   106	        stream_output: bool = False,
   107	    ) -> subprocess.CompletedProcess[str]:
   108	        result = self._run_transport_command(
   109	            [*self._ssh_command_prefix(), 'sh -lc "$HOME/.talon/bin/repl"'],
   110	            input_text=payload,
   111	        )
   112	        if stream_output or result.returncode != 0:
   113	            if result.stdout:
   114	                sys.stdout.write(result.stdout)
   115	            if result.stderr:
   116	                sys.stderr.write(result.stderr)
   117	        return result
   118	
   119	    def wait_for_talon_repl(
   120	        self,
   121	        *,
   122	        timeout: float = TALON_REPL_TIMEOUT_SECONDS,
   123	    ) -> None:
   124	        self.run_shell(
   125	            'test -S "$HOME/.talon/.sys/repl.sock"',
   126	            timeout=timeout,
   127	            poll=True,
   128	        )
   129	
   130	    def probe_ssh(self, *, timeout: float = SSH_TIMEOUT_SECONDS) -> None:
   131	        self.run_shell(
   132	            "true",
   133	            timeout=timeout,
   134	            poll=True,
   135	        )
   136	
   137	    def download(self, remote_path: str, local_path: Path) -> None:
   138	        result = self._run_transport_command(
   139	            [
   140	                *self.scp_command_prefix(),
   141	                self.ssh_remote_path(remote_path),
   142	                str(local_path),
   143	            ],
   144	        )
   145	        if result.returncode != 0:
   146	            message = result.stderr.strip() or result.stdout.strip()
   147	            if not message:
   148	                message = "failed to download file from guest"
   149	            raise TransportError(message)
   150	
   151	    def ssh_remote_path(self, guest_path: str) -> str:
   152	        return f"{self.SSH_USERNAME}@{self.ip_address}:{guest_path}"
   153	
   154	    def restart_talon(
   155	        self,
   156	        *,
   157	        wipe_user_dir: bool,
   158	        clean_logs: bool,
   159	    ) -> None:
   160	        self.run_shell("pkill -x Talon >/dev/null 2>&1 || true")
   161	        if clean_logs:
   162	            self.run_shell(
   163	                f'mkdir -p "$HOME/.talon" && : > {TALON_LOG} && : > /tmp/talonbox-talon.log'
   164	            )
   165	        self.run_shell('mkdir -p "$HOME/.talon/user"')
   166	        if wipe_user_dir:
   167	            self.run_shell(
   168	                'find "$HOME/.talon/user" -mindepth 1 -maxdepth 1 -exec rm -rf {} +'
   169	            )
   170	        script_path = "/tmp/talonbox-launch.command"
   171	        script_body = f"#!/bin/sh\nexec arch -x86_64 {TALON_BINARY} >/tmp/talonbox-talon.log 2>&1\n"
   172	        self.run_shell(
   173	            f"printf %s {shlex.quote(script_body)} > {shlex.quote(script_path)} && "
   174	            f"chmod +x {shlex.quote(script_path)} && "
   175	            f"open -a Terminal {shlex.quote(script_path)}"
   176	        )
   177	        self.run_shell(
   178	            "pgrep -x Talon >/dev/null",
   179	            timeout=TALON_TIMEOUT_SECONDS,
   180	            poll=True,
   181	        )
   182	        self.wait_for_talon_repl(timeout=TALON_REPL_TIMEOUT_SECONDS)
   183	        time.sleep(TALON_POST_RESTART_SETTLE_SECONDS)
   184	
   185	    def logout_guest_session(self) -> None:
   186	        self.run_shell(
   187	            (
   188	                "launchctl bootout gui/$(id -u) >/dev/null 2>&1 || true; "
   189	                "while pgrep -x Talon >/dev/null 2>&1; do sleep 1; done"
   190	            ),
   191	            timeout=15.0,
   192	        )
   193	
   194	    def _ssh_command_prefix(self) -> list[str]:
   195	        return [
   196	            "sshpass",
   197	            "-p",
   198	            self.SSH_PASSWORD,
   199	            "ssh",
   200	            *self.SSH_OPTIONS,
   201	            f"{self.SSH_USERNAME}@{self.ip_address}",
   202	        ]
   203	
   204	    def scp_command_prefix(self) -> list[str]:
   205	        return ["sshpass", "-p", self.SSH_PASSWORD, "scp", *self.SSH_OPTIONS]
   206	
   207	    def ssh_command_for_rsync(self) -> str:
   208	        return shlex.join(
   209	            ["sshpass", "-p", self.SSH_PASSWORD, "ssh", *self.SSH_OPTIONS]
   210	        )
   211	
   212	    def _run_transport_command(
   213	        self,
   214	        cmd: list[str],
   215	        *,
   216	        timeout: float | None = None,
   217	        poll: bool = False,
   218	        stream: bool = False,
   219	        input_text: str | None = None,
   220	    ) -> subprocess.CompletedProcess[str]:
```

`RunningVm` is where transport policy lives. The SSH username and password are hard-coded because this tool targets a known disposable Lume guest rather than arbitrary machines. `run_shell()` converts either a string or argv-style list into `sh -lc ...` on the guest, then funnels everything through `_run_transport_command()`. `run_repl()` does the same for Talon's REPL binary.

The implementation tries hard to smooth over the flaky edge of a VM just after boot. `_run_transport_command()` can poll until a deadline and it has a narrow transient-retry list for SSH errors such as `connection refused`, `broken pipe`, and the familiar `ssh_askpass` password prompt failure. That makes the higher-level startup sequence much less brittle.

```bash
nl -ba src/talonbox/vm.py | sed -n '220,420p'
```

```output
   220	    ) -> subprocess.CompletedProcess[str]:
   221	        if self.debug:
   222	            click.echo(f"+ {shlex.join(cmd)}", err=True)
   223	
   224	        deadline = time.monotonic() + timeout if poll and timeout is not None else None
   225	        attempts = 0
   226	        while True:
   227	            result = subprocess.run(
   228	                cmd,
   229	                check=False,
   230	                text=True,
   231	                capture_output=not stream,
   232	                timeout=None if poll else timeout,
   233	                stdin=None if input_text is not None else subprocess.DEVNULL,
   234	                input=input_text,
   235	            )
   236	            if result.returncode == 0 or not poll:
   237	                if result.returncode == 0:
   238	                    return result
   239	                if attempts < TRANSIENT_RETRY_ATTEMPTS:
   240	                    message = (result.stderr.strip() or result.stdout.strip()).lower()
   241	                    if any(
   242	                        needle in message
   243	                        for needle in (
   244	                            "ssh_askpass",
   245	                            "permission denied (publickey,password,keyboard-interactive)",
   246	                            "connection reset by peer",
   247	                            "connection refused",
   248	                            "connection closed by remote host",
   249	                            "operation timed out",
   250	                            "no route to host",
   251	                            "kex_exchange_identification",
   252	                            "broken pipe",
   253	                        )
   254	                    ):
   255	                        attempts += 1
   256	                        time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
   257	                        continue
   258	                return result
   259	            if deadline is not None and time.monotonic() >= deadline:
   260	                return result
   261	            time.sleep(2.0)
   262	
   263	
   264	class VmController:
   265	    def __init__(self, vm: str, debug: bool) -> None:
   266	        self.vm = vm
   267	        self.debug = debug
   268	
   269	    def debug_log(self, message: str) -> None:
   270	        if self.debug:
   271	            click.echo(message, err=True)
   272	
   273	    def get_vm(self) -> lume.VmInfo:
   274	        try:
   275	            info = lume.get_vm_info(self.vm, debug=self.debug)
   276	        except lume.LumeError as error:
   277	            raise click.ClickException(str(error)) from None
   278	        if info is None:
   279	            raise click.ClickException(f"VM not found: {self.vm}")
   280	        return info
   281	
   282	    def get_running_vm(self) -> RunningVm:
   283	        info = self.get_vm()
   284	        return self._running_vm_from_info(info)
   285	
   286	    def format_vm_info(self, info: lume.VmInfo) -> list[str]:
   287	        lines = [f"status: {info.status}"]
   288	        if info.status == "running" and info.ip_address:
   289	            lines.extend(
   290	                [
   291	                    f"ip: {info.ip_address}",
   292	                    f"username: {RunningVm.SSH_USERNAME}",
   293	                    f"password: {RunningVm.SSH_PASSWORD}",
   294	                ]
   295	            )
   296	            if info.vnc_url:
   297	                lines.append(f"vnc: {info.vnc_url}")
   298	        return lines
   299	
   300	    def start(self) -> RunningVm:
   301	        info = self.get_vm()
   302	        if info.status == "running":
   303	            raise click.ClickException(f"VM is already running: {self.vm}")
   304	        if info.status != "stopped":
   305	            raise click.ClickException(f"VM is not stopped: {self.vm} ({info.status})")
   306	
   307	        launch = None
   308	        try:
   309	            launch = lume.spawn_vm(self.vm, debug=self.debug)
   310	            ready_info = lume.wait_for_running_vm(
   311	                self.vm,
   312	                timeout=START_TIMEOUT_SECONDS,
   313	                debug=self.debug,
   314	                launch=launch,
   315	            )
   316	            running_vm = self._running_vm_from_info(ready_info)
   317	            running_vm.probe_ssh(timeout=SSH_TIMEOUT_SECONDS)
   318	            running_vm.restart_talon(wipe_user_dir=True, clean_logs=True)
   319	        except (lume.LumeError, RemoteCommandError, TransportError) as error:
   320	            if launch is not None and launch.process.poll() is None:
   321	                self._cleanup_failed_start()
   322	            raise click.ClickException(str(error)) from None
   323	
   324	        lume.cleanup_launch_log(launch.log_path)
   325	        return running_vm
   326	
   327	    def restart_talon(
   328	        self,
   329	        *,
   330	        wipe_user_dir: bool,
   331	        clean_logs: bool,
   332	    ) -> None:
   333	        self.get_running_vm().restart_talon(
   334	            wipe_user_dir=wipe_user_dir,
   335	            clean_logs=clean_logs,
   336	        )
   337	
   338	    def stop(self) -> None:
   339	        info = self.get_vm()
   340	        if info.status == "stopped":
   341	            return
   342	
   343	        if info.status == "running" and info.ip_address:
   344	            try:
   345	                self._running_vm_from_info(info).logout_guest_session()
   346	            except (RemoteCommandError, TransportError) as error:
   347	                self.debug_log(f"guest logout failed: {error}")
   348	        try:
   349	            lume.stop_vm(self.vm, debug=self.debug)
   350	            lume.wait_for_status(self.vm, "stopped", timeout=60.0, debug=self.debug)
   351	        except lume.LumeError as error:
   352	            self.debug_log(f"graceful stop failed: {error}")
   353	            try:
   354	                lume.force_stop_vm(self.vm, debug=self.debug)
   355	                lume.wait_for_status(self.vm, "stopped", timeout=20.0, debug=self.debug)
   356	            except lume.LumeError as force_error:
   357	                raise click.ClickException(str(force_error)) from None
   358	
   359	    def _cleanup_failed_start(self) -> None:
   360	        self.debug_log("start failed; stopping VM")
   361	        try:
   362	            lume.stop_vm(self.vm, debug=self.debug)
   363	            lume.wait_for_status(self.vm, "stopped", timeout=30.0, debug=self.debug)
   364	        except lume.LumeError as error:
   365	            self.debug_log(f"cleanup stop failed: {error}")
   366	
   367	    def _running_vm_from_info(self, info: lume.VmInfo) -> RunningVm:
   368	        if info.status != "running" or not info.ip_address:
   369	            raise click.ClickException(f"VM is not running: {self.vm}")
   370	        return RunningVm(
   371	            name=info.name,
   372	            ip_address=info.ip_address,
   373	            debug=self.debug,
   374	            vnc_url=info.vnc_url,
   375	        )
```

The most important method in this file is `VmController.start()`. Read it as a checklist. It confirms the VM exists and is stopped, spawns `lume run`, waits for a running VM with an IP address, probes SSH, and only then restarts Talon with a wiped user directory and cleaned logs. That last step is what turns a generic VM boot into a reproducible Talon sandbox reset.

`RunningVm.restart_talon()` is also packed with intent. It kills any existing Talon process, optionally truncates logs, ensures `~/.talon/user` exists, optionally wipes that directory, writes a tiny launcher script under `/tmp`, and opens it in Terminal under Rosetta. Launching through Terminal preserves the guest-side permissions needed for screenshots. Once the process appears, the method waits for Talon's repl socket and then gives it a short settle delay.

`VmController.stop()` mirrors that care on the way down. It tries to log out the guest GUI session, attempts a graceful stop through `lume`, waits for the status change, and only falls back to force-stop if the graceful path times out or errors.

## 5. `transfer.py` Enforces the Host Safety Boundary

`src/talonbox/transfer.py` is one of the most security-sensitive modules in the repo. The top-level user promise from the README and AGENTS file is that callers cannot use `talonbox` to trigger arbitrary host writes outside `/tmp`. This module is where that promise becomes actual argument validation and a macOS sandbox profile.

```bash
nl -ba src/talonbox/transfer.py | sed -n '1,220p'
```

```output
     1	from __future__ import annotations
     2	
     3	import shlex
     4	import shutil
     5	import subprocess
     6	from collections.abc import Sequence
     7	from dataclasses import dataclass
     8	from pathlib import Path
     9	
    10	import click
    11	
    12	from .vm import RunningVm
    13	
    14	HOST_OUTPUT_ROOT = Path("/tmp")
    15	GUEST_PREFIX = "guest:"
    16	DEVICE_ROOT = Path("/dev")
    17	RSYNC_VALUE_OPTIONS = {
    18	    "-B",
    19	    "-f",
    20	    "-M",
    21	    "-T",
    22	    "--backup-dir",
    23	    "--block-size",
    24	    "--bwlimit",
    25	    "--chmod",
    26	    "--compare-dest",
    27	    "--compress-choice",
    28	    "--copy-dest",
    29	    "--exclude",
    30	    "--exclude-from",
    31	    "--files-from",
    32	    "--filter",
    33	    "--iconv",
    34	    "--include",
    35	    "--include-from",
    36	    "--link-dest",
    37	    "--log-file",
    38	    "--log-file-format",
    39	    "--max-size",
    40	    "--min-size",
    41	    "--out-format",
    42	    "--partial-dir",
    43	    "--password-file",
    44	    "--skip-compress",
    45	    "--suffix",
    46	    "--temp-dir",
    47	}
    48	RSYNC_REJECTED_OPTIONS = {
    49	    "-e",
    50	    "--rsync-path",
    51	    "--rsh",
    52	}
    53	SCP_VALUE_OPTIONS = {"-c", "-D", "-i", "-l", "-o", "-P", "-S", "-X"}
    54	SCP_REJECTED_OPTIONS = {"-F", "-J", "-o", "-S"}
    55	
    56	
    57	@dataclass(frozen=True, slots=True)
    58	class TransferOperand:
    59	    raw: str
    60	    kind: str
    61	    path: str
    62	
    63	
    64	class TransferService:
    65	    def __init__(self, running_vm: RunningVm) -> None:
    66	        self.running_vm = running_vm
    67	
    68	    def prepare_rsync_args(self, args: Sequence[str]) -> list[str]:
    69	        return self._build_transfer_command_args(
    70	            args,
    71	            self.running_vm,
    72	            value_options=RSYNC_VALUE_OPTIONS,
    73	            rejected_options=RSYNC_REJECTED_OPTIONS,
    74	        )
    75	
    76	    def prepare_scp_args(self, args: Sequence[str]) -> list[str]:
    77	        return self._build_transfer_command_args(
    78	            args,
    79	            self.running_vm,
    80	            value_options=SCP_VALUE_OPTIONS,
    81	            rejected_options=SCP_REJECTED_OPTIONS,
    82	        )
    83	
    84	    def rsync(self, args: Sequence[str]) -> int:
    85	        return self._run_transfer(
    86	            [
    87	                *self._sandbox_command_prefix(),
    88	                "rsync",
    89	                "-e",
    90	                self.running_vm.ssh_command_for_rsync(),
    91	                *self._build_transfer_command_args(
    92	                    args,
    93	                    self.running_vm,
    94	                    value_options=RSYNC_VALUE_OPTIONS,
    95	                    rejected_options=RSYNC_REJECTED_OPTIONS,
    96	                ),
    97	            ],
    98	        )
    99	
   100	    def scp(self, args: Sequence[str]) -> int:
   101	        return self._run_transfer(
   102	            [
   103	                *self._sandbox_command_prefix(),
   104	                *self.running_vm.scp_command_prefix(),
   105	                *self._build_transfer_command_args(
   106	                    args,
   107	                    self.running_vm,
   108	                    value_options=SCP_VALUE_OPTIONS,
   109	                    rejected_options=SCP_REJECTED_OPTIONS,
   110	                ),
   111	            ]
   112	        )
   113	
   114	    def normalize_local_output_path(self, raw_path: str | Path) -> Path:
   115	        destination = Path(raw_path).expanduser()
   116	        if not destination.is_absolute():
   117	            destination = Path.cwd() / destination
   118	
   119	        try:
   120	            resolved_destination = destination.resolve(strict=False)
   121	        except (OSError, RuntimeError) as error:
   122	            raise click.ClickException(
   123	                f"Unable to resolve local output path {raw_path!s}: {error}"
   124	            ) from None
   125	
   126	        host_output_root = self._host_output_root()
   127	        if self._is_relative_to(resolved_destination, host_output_root):
   128	            return resolved_destination
   129	
   130	        raise click.ClickException(
   131	            "Local output paths must stay under /tmp. "
   132	            "Symlinks that escape /tmp are not allowed."
   133	        )
   134	
   135	    def _build_transfer_command_args(
   136	        self,
   137	        args: Sequence[str],
   138	        running_vm: RunningVm,
   139	        *,
   140	        value_options: set[str],
   141	        rejected_options: set[str],
   142	    ) -> list[str]:
   143	        passthrough, positionals = self._split_transfer_options_and_operands(
   144	            args,
   145	            value_options=value_options,
   146	            rejected_options=rejected_options,
   147	        )
   148	        if len(positionals) < 2:
   149	            raise click.ClickException(
   150	                "Transfer requires at least one source and one destination"
   151	            )
   152	
   153	        sources = [self._classify_transfer_operand(arg) for arg in positionals[:-1]]
   154	        destination = self._classify_transfer_operand(positionals[-1])
   155	
   156	        source_kinds = {source.kind for source in sources}
   157	        if len(source_kinds) != 1:
   158	            raise click.ClickException("Mixed local and guest sources are not allowed")
   159	        source_kind = next(iter(source_kinds))
   160	        if source_kind == destination.kind:
   161	            if source_kind == "local":
   162	                raise click.ClickException(
   163	                    "Local-to-local transfers are not allowed; use guest: paths for the VM"
   164	                )
   165	            raise click.ClickException("Guest-to-guest transfers are not allowed")
   166	        if destination.kind == "local":
   167	            destination = TransferOperand(
   168	                raw=destination.raw,
   169	                kind=destination.kind,
   170	                path=str(self.normalize_local_output_path(destination.path)),
   171	            )
   172	
   173	        rewritten = [
   174	            self._rewrite_transfer_operand(running_vm, operand)
   175	            for operand in [*sources, destination]
   176	        ]
   177	        return [*passthrough, *rewritten]
   178	
   179	    def _split_transfer_options_and_operands(
   180	        self,
   181	        args: Sequence[str],
   182	        *,
   183	        value_options: set[str],
   184	        rejected_options: set[str],
   185	    ) -> tuple[list[str], list[str]]:
   186	        passthrough: list[str] = []
   187	        positionals: list[str] = []
   188	        index = 0
   189	        parsing_options = True
   190	
   191	        while index < len(args):
   192	            arg = args[index]
   193	            if parsing_options and arg == "--":
   194	                passthrough.append(arg)
   195	                parsing_options = False
   196	                index += 1
   197	                continue
   198	            if not parsing_options or not arg.startswith("-") or arg == "-":
   199	                positionals.append(arg)
   200	                index += 1
   201	                continue
   202	
   203	            if arg.startswith("--"):
   204	                option, has_value, _ = arg.partition("=")
   205	                if option in rejected_options:
   206	                    raise click.ClickException(
   207	                        f"Option not allowed for VM-only transfer safety: {option}"
   208	                    )
   209	                passthrough.append(arg)
   210	                index += 1
   211	                if has_value or option not in value_options:
   212	                    continue
   213	                if index >= len(args):
   214	                    raise click.ClickException(f"Option requires a value: {option}")
   215	                passthrough.append(args[index])
   216	                index += 1
   217	                continue
   218	
   219	            short_option = arg[:2]
   220	            if short_option in rejected_options:
```

The first half of the file is mostly about normalizing transfer arguments before any subprocess is launched. `_split_transfer_options_and_operands()` walks the raw argv stream, preserving allowed flags, rejecting transport override flags like `-e` for rsync or `-F` for scp, and keeping enough structure to distinguish options from operands. `_classify_transfer_operand()` then insists that remote paths use the explicit `guest:/...` syntax and rejects any other colon-bearing remote form.

Once operands are classified, `_build_transfer_command_args()` applies the core policy. All sources must be either local or guest-side, never mixed. One side must be local and the other guest-side. If the destination is local, `normalize_local_output_path()` resolves symlinks and rejects anything that does not stay under the canonical host output root. That is how the code blocks both obvious writes outside `/tmp` and symlink escapes that start inside `/tmp` but resolve elsewhere.

```bash
nl -ba src/talonbox/transfer.py | sed -n '220,340p'
```

```output
   220	            if short_option in rejected_options:
   221	                raise click.ClickException(
   222	                    f"Option not allowed for VM-only transfer safety: {short_option}"
   223	                )
   224	            passthrough.append(arg)
   225	            index += 1
   226	            if short_option not in value_options or len(arg) > 2:
   227	                continue
   228	            if index >= len(args):
   229	                raise click.ClickException(f"Option requires a value: {short_option}")
   230	            passthrough.append(args[index])
   231	            index += 1
   232	
   233	        return passthrough, positionals
   234	
   235	    def _classify_transfer_operand(self, raw: str) -> TransferOperand:
   236	        if raw.startswith(GUEST_PREFIX):
   237	            path = raw[len(GUEST_PREFIX) :]
   238	            if not path:
   239	                raise click.ClickException("Guest path must not be empty: guest:/path")
   240	            if not path.startswith("/"):
   241	                raise click.ClickException(f"Guest path must be absolute: {raw}")
   242	            return TransferOperand(raw=raw, kind="guest", path=path)
   243	        if raw.startswith("rsync://"):
   244	            raise click.ClickException(f"Only guest: remote paths are allowed: {raw}")
   245	        if ":" in raw:
   246	            raise click.ClickException(f"Only guest: remote paths are allowed: {raw}")
   247	        return TransferOperand(raw=raw, kind="local", path=raw)
   248	
   249	    def _rewrite_transfer_operand(
   250	        self, running_vm: RunningVm, operand: TransferOperand
   251	    ) -> str:
   252	        if operand.kind == "local":
   253	            return operand.path
   254	        return running_vm.ssh_remote_path(operand.path)
   255	
   256	    def _host_output_root(self) -> Path:
   257	        return HOST_OUTPUT_ROOT.resolve(strict=False)
   258	
   259	    def _is_relative_to(self, path: Path, root: Path) -> bool:
   260	        try:
   261	            path.relative_to(root)
   262	            return True
   263	        except ValueError:
   264	            return False
   265	
   266	    def _sandbox_command_prefix(self) -> list[str]:
   267	        sandbox_exec = shutil.which("sandbox-exec")
   268	        if sandbox_exec is None:
   269	            raise click.ClickException(
   270	                "sandbox-exec is required on macOS to enforce talonbox host write boundaries."
   271	            )
   272	
   273	        return [sandbox_exec, "-p", self._sandbox_profile()]
   274	
   275	    def _sandbox_profile(self) -> str:
   276	        host_output_root = self._host_output_root()
   277	        writable_roots = {host_output_root}
   278	        if host_output_root != HOST_OUTPUT_ROOT:
   279	            writable_roots.add(HOST_OUTPUT_ROOT)
   280	
   281	        write_rules = [
   282	            f'(allow file-write* (subpath "{root}"))' for root in sorted(writable_roots)
   283	        ]
   284	        write_rules.append(f'(allow file-write* (subpath "{DEVICE_ROOT}"))')
   285	        return " ".join(
   286	            [
   287	                "(version 1)",
   288	                "(allow default)",
   289	                "(deny file-write*)",
   290	                *write_rules,
   291	            ]
   292	        )
   293	
   294	    def _run_transfer(self, cmd: list[str]) -> int:
   295	        if self.running_vm.debug:
   296	            click.echo(f"+ {shlex.join(cmd)}", err=True)
   297	        result = subprocess.run(cmd, check=False)
   298	        if result.returncode and self._sandbox_command_prefix():
   299	            click.echo(
   300	                "HINT transfers run inside a macOS sandbox; extra host-side writes "
   301	                "outside /tmp fail with 'Operation not permitted'.",
   302	                err=True,
   303	            )
   304	        return result.returncode
```

The second half is where enforcement moves from validation into execution. `_sandbox_command_prefix()` requires `sandbox-exec` and builds a profile that denies all writes except under the allowed temp roots and `/dev`. `rsync()` and `scp()` then prepend that sandbox wrapper before launching the real transfer subprocess.

That design is stronger than argument filtering alone. Even if an underlying tool gains a new write-capable option or a caller finds an unusual combination of flags, the host-side macOS sandbox still constrains where writes can land. The validation layer explains mistakes clearly; the sandbox layer is the backstop.

## 6. `talon_client.py` Wraps the Talon-Native Operations

`src/talonbox/talon_client.py` is small, but it gives the top-level commands a much clearer shape. Instead of making the CLI know how to wait for the Talon repl socket, construct mimic payloads, or manage screenshot temp files, those behaviors live behind `repl()`, `mimic()`, and `capture_screenshot()`.

```bash
nl -ba src/talonbox/talon_client.py | sed -n '1,200p'
```

```output
     1	from __future__ import annotations
     2	
     3	import uuid
     4	from pathlib import Path
     5	
     6	import click
     7	
     8	from .transfer import TransferService
     9	from .vm import RemoteCommandError, RunningVm, TransportError
    10	
    11	
    12	class TalonClient:
    13	    def __init__(
    14	        self, running_vm: RunningVm, transfer_service: TransferService
    15	    ) -> None:
    16	        self.running_vm = running_vm
    17	        self.transfer_service = transfer_service
    18	
    19	    def repl(self, code: str) -> None:
    20	        self.running_vm.wait_for_talon_repl()
    21	        result = self.running_vm.run_repl(
    22	            f"exec({code!r})\n",
    23	            stream_output=True,
    24	        )
    25	        if result.returncode:
    26	            raise click.exceptions.Exit(result.returncode)
    27	
    28	    def mimic(self, command: str) -> None:
    29	        self.running_vm.wait_for_talon_repl()
    30	        result = self.running_vm.run_repl(
    31	            f"mimic({command!r})\n",
    32	        )
    33	        if result.returncode:
    34	            raise click.exceptions.Exit(result.returncode)
    35	
    36	    def capture_screenshot(self, filepath: Path) -> None:
    37	        filepath = self.transfer_service.normalize_local_output_path(filepath)
    38	        filepath.parent.mkdir(parents=True, exist_ok=True)
    39	        remote_path = f"/tmp/talonbox-screenshot-{uuid.uuid4().hex}.png"
    40	        try:
    41	            self.running_vm.wait_for_talon_repl()
    42	            result = self.running_vm.run_repl(
    43	                "\n".join(
    44	                    [
    45	                        "from talon import screen",
    46	                        f"path = {remote_path!r}",
    47	                        "img = screen.capture_rect(screen.main().rect, retina=False)",
    48	                        "img.save(path) if hasattr(img, 'save') else img.write_file(path)",
    49	                        "print(path)",
    50	                        "",
    51	                    ]
    52	                ),
    53	            )
    54	            if result.returncode:
    55	                raise click.exceptions.Exit(result.returncode)
    56	            self.running_vm.download(remote_path, filepath)
    57	        except (RemoteCommandError, TransportError) as error:
    58	            raise click.ClickException(str(error)) from None
    59	        finally:
    60	            try:
    61	                self.running_vm.run_shell(
    62	                    f'rm -f "{remote_path}"',
    63	                )
    64	            except (RemoteCommandError, TransportError):
    65	                pass
```

All three methods begin from the same assumption: Talon is only usable once the repl socket exists. `repl()` wraps arbitrary caller code in `exec(...)` so multiline Python can be shipped as a single payload. `mimic()` turns a voice phrase into `mimic(<phrase>)`. `capture_screenshot()` is a fuller workflow: validate the host output path, generate a unique guest temp path, run a small Talon screen-capture script inside the guest, download the resulting image, and finally best-effort delete the guest temp file.

That layering is important because screenshots are the one Talon-specific operation that crosses the guest-host boundary in a structured way. `TalonClient` delegates the boundary check to `TransferService` and the transport mechanics to `RunningVm`, which keeps responsibilities clean.

## 7. `smoke_test.py` Shows the Intended End-to-End Workflow

If you want one file that explains how the pieces are supposed to collaborate in practice, `src/talonbox/smoke_test.py` is it. The smoke test is not just a health check. It is a scripted rehearsal of the whole product promise: get to a clean VM, stage Talon content, prove a Talon action can run, prove screenshots work, and leave behind artifacts that make failures debuggable.

```bash
nl -ba src/talonbox/smoke_test.py | sed -n '1,220p'
```

```output
     1	from __future__ import annotations
     2	
     3	import shlex
     4	import uuid
     5	from collections.abc import Callable
     6	from pathlib import Path
     7	
     8	import click
     9	
    10	from . import lume
    11	from .talon_client import TalonClient
    12	from .transfer import HOST_OUTPUT_ROOT, TransferService
    13	from .vm import RunningVm, VmController
    14	
    15	
    16	class SmokeTestRunner:
    17	    def __init__(
    18	        self,
    19	        vm_controller: VmController,
    20	        *,
    21	        host_output_root: Path = HOST_OUTPUT_ROOT,
    22	    ) -> None:
    23	        self.vm_controller = vm_controller
    24	        self.host_output_root = host_output_root
    25	        self._hint_screenshot: Callable[[], Path | None] | None = None
    26	
    27	    def run(
    28	        self,
    29	        *,
    30	        yes: bool,
    31	        confirm: Callable[..., bool] = click.confirm,
    32	    ) -> None:
    33	        artifact_dir = self.host_output_root / f"talonbox-smoke-test-{uuid.uuid4().hex}"
    34	        artifact_dir.mkdir(parents=True, exist_ok=True)
    35	        baseline_screenshot_path = artifact_dir / "screenshot-before-dialog.png"
    36	        screenshot_path = artifact_dir / "screenshot-after-dialog.png"
    37	        bundle_dir = artifact_dir / "bundle"
    38	        marker_path = f"/tmp/talonbox-smoke-test-marker-{uuid.uuid4().hex}.txt"
    39	        token = uuid.uuid4().hex
    40	        started = False
    41	
    42	        def hint_screenshot() -> Path | None:
    43	            if screenshot_path.exists():
    44	                return screenshot_path
    45	            if baseline_screenshot_path.exists():
    46	                return baseline_screenshot_path
    47	            return None
    48	
    49	        self._hint_screenshot = hint_screenshot
    50	        self.log("ARTIFACT", artifact_dir)
    51	
    52	        try:
    53	            info = self.run_step(
    54	                "Inspect VM status",
    55	                self.vm_controller.get_vm,
    56	                success_message="VM status checked.",
    57	            )
    58	            assert isinstance(info, lume.VmInfo)
    59	            if info.status not in {"running", "stopped"}:
    60	                raise click.ClickException(
    61	                    f"VM is not ready for smoke-test: {self.vm_controller.vm} ({info.status})"
    62	                )
    63	
    64	            if info.status == "running":
    65	                message = (
    66	                    f"VM {self.vm_controller.vm} is already running. smoke-test must stop "
    67	                    "and restart it before continuing."
    68	                )
    69	                click.echo(message)
    70	                if not yes and not confirm("Continue with smoke-test?", default=False):
    71	                    self.log("FAIL", "smoke-test canceled by user; VM left running.")
    72	                    raise click.exceptions.Exit(1)
    73	                self.run_step(
    74	                    "Stop the running VM before smoke-test",
    75	                    self.vm_controller.stop,
    76	                    success_message="Running VM stopped.",
    77	                )
    78	
    79	            running_vm = self.run_step(
    80	                "Start the VM and reset Talon",
    81	                self.vm_controller.start,
    82	                success_message="VM started and Talon reset.",
    83	            )
    84	            assert isinstance(running_vm, RunningVm)
    85	            started = True
    86	            transfer_service = self._build_transfer_service(running_vm)
    87	            talon_client = self._build_talon_client(running_vm, transfer_service)
    88	
    89	            self.run_step(
    90	                "Write the temporary Talon smoke-test bundle",
    91	                lambda: self.write_bundle(bundle_dir, marker_path, token),
    92	                success_message="Temporary Talon bundle written.",
    93	            )
    94	            self.run_step(
    95	                "Upload the Talon smoke-test bundle with rsync",
    96	                lambda: self.upload_bundle(transfer_service, bundle_dir),
    97	                success_message="Temporary Talon bundle uploaded.",
    98	            )
    99	            self.run_step(
   100	                "Restart Talon to load the uploaded bundle",
   101	                lambda: self.vm_controller.restart_talon(
   102	                    wipe_user_dir=False,
   103	                    clean_logs=True,
   104	                ),
   105	                success_message="Talon restarted after upload.",
   106	            )
   107	            self.run_step(
   108	                "Run mimic for 'talonbox smoke test'",
   109	                lambda: talon_client.mimic("talonbox smoke test"),
   110	                success_message="mimic succeeded.",
   111	            )
   112	            self.run_step(
   113	                "Verify the guest smoke-test marker",
   114	                lambda: self.verify_marker(running_vm, marker_path, token),
   115	                success_message="Guest marker verified.",
   116	            )
   117	            self.run_step(
   118	                "Capture a baseline screenshot",
   119	                lambda: talon_client.capture_screenshot(baseline_screenshot_path),
   120	                success_message="Baseline screenshot captured.",
   121	            )
   122	            self.run_step(
   123	                "Validate the baseline screenshot artifact",
   124	                lambda: self.validate_screenshot(baseline_screenshot_path),
   125	                success_message="Baseline screenshot artifact validated.",
   126	            )
   127	            self.run_step(
   128	                "Trigger a visible guest dialog",
   129	                lambda: self.trigger_visual_change(running_vm, token),
   130	                success_message="Guest dialog triggered.",
   131	            )
   132	            self.run_step(
   133	                "Capture a second screenshot after the guest dialog",
   134	                lambda: talon_client.capture_screenshot(screenshot_path),
   135	                success_message="Second screenshot captured.",
   136	            )
   137	            self.run_step(
   138	                "Validate the second screenshot artifact",
   139	                lambda: self.validate_screenshot(screenshot_path),
   140	                success_message="Second screenshot artifact validated.",
   141	            )
   142	            self.run_step(
   143	                "Verify the screenshots changed after the guest dialog",
   144	                lambda: self.verify_screenshots_differ(
   145	                    baseline_screenshot_path, screenshot_path
   146	                ),
   147	                success_message="Screenshots changed after the guest dialog.",
   148	            )
   149	        except click.ClickException as error:
   150	            self._fail(str(error), screenshot_path=hint_screenshot())
   151	        except click.exceptions.Exit:
   152	            raise
   153	        finally:
   154	            self._hint_screenshot = None
   155	            if started:
   156	                self.run_step(
   157	                    "Stop the VM after smoke-test",
   158	                    self.vm_controller.stop,
   159	                    success_message="VM stopped after smoke-test.",
   160	                )
   161	
   162	        self.log("PASS", "Smoke test completed successfully.")
   163	
   164	    def write_bundle(self, bundle_dir: Path, marker_path: str, token: str) -> None:
   165	        bundle_dir.mkdir(parents=True, exist_ok=True)
   166	        (bundle_dir / "talonbox_smoke_test.talon").write_text(
   167	            "-\ntalonbox smoke test:\n    user.talonbox_smoke_test()\n",
   168	            encoding="utf-8",
   169	        )
   170	        (bundle_dir / "talonbox_smoke_test.py").write_text(
   171	            "\n".join(
   172	                [
   173	                    "from pathlib import Path",
   174	                    "",
   175	                    "from talon import Module",
   176	                    "",
   177	                    "mod = Module()",
   178	                    "",
   179	                    "@mod.action_class",
   180	                    "class Actions:",
   181	                    "    def talonbox_smoke_test() -> None:",
   182	                    '        """Write the talonbox smoke-test marker file."""',
   183	                    f"        Path({marker_path!r}).write_text({token!r}, encoding='utf-8')",
   184	                    "",
   185	                ]
   186	            ),
   187	            encoding="utf-8",
   188	        )
   189	
   190	    def upload_bundle(
   191	        self, transfer_service: TransferService, bundle_dir: Path
   192	    ) -> None:
   193	        returncode = transfer_service.rsync(
   194	            [
   195	                "-av",
   196	                f"{bundle_dir}/",
   197	                "guest:/Users/lume/.talon/user/talonbox_smoke_test/",
   198	            ]
   199	        )
   200	        if returncode:
   201	            raise click.ClickException(f"rsync failed with exit code {returncode}")
   202	
   203	    def verify_marker(
   204	        self, running_vm: RunningVm, marker_path: str, token: str
   205	    ) -> None:
   206	        result = running_vm.run_shell(
   207	            ["cat", marker_path],
   208	            check=False,
   209	        )
   210	        if result.returncode != 0:
   211	            raise click.ClickException(
   212	                result.stderr.strip()
   213	                or result.stdout.strip()
   214	                or f"Smoke test marker was not created: {marker_path}"
   215	            )
   216	        if result.stdout.strip() != token:
   217	            raise click.ClickException(
   218	                f"Smoke test marker contents did not match expected token: {marker_path}"
   219	            )
   220	
```

The first half of `SmokeTestRunner.run()` reads almost like a test plan written in executable form. It creates an artifact directory under the allowed host output root, derives the guest marker path and a random token, records a screenshot-hint callback for future failure messages, then moves step by step through inspection, optional confirmation, startup, bundle creation, bundle upload, Talon restart, mimic, guest verification, and screenshot capture.

`write_bundle()` is a nice example of how little Talon code the system needs to prove its point. The temporary `.talon` file maps the spoken phrase `talonbox smoke test` to a custom action, and the paired Python file implements that action by writing a token to a marker path in the guest. If `mimic()` succeeds and the token appears, the whole voice-command path is working.

```bash
nl -ba src/talonbox/smoke_test.py | sed -n '220,340p'
```

```output
   220	
   221	    def validate_screenshot(self, path: Path) -> None:
   222	        if not path.exists():
   223	            raise click.ClickException(f"Smoke test screenshot was not created: {path}")
   224	        if path.stat().st_size <= 0:
   225	            raise click.ClickException(f"Smoke test screenshot was empty: {path}")
   226	        with path.open("rb") as handle:
   227	            signature = handle.read(8)
   228	        if signature != b"\x89PNG\r\n\x1a\n":
   229	            raise click.ClickException(
   230	                f"Smoke test screenshot was not a PNG file: {path}"
   231	            )
   232	
   233	    def trigger_visual_change(self, running_vm: RunningVm, token: str) -> None:
   234	        dialog_log = f"/tmp/talonbox-smoke-test-dialog-{token}.log"
   235	        script = (
   236	            f'display dialog "talonbox screenshot test {token}" '
   237	            'buttons {"OK"} default button 1 giving up after 15'
   238	        )
   239	        running_vm.run_shell(
   240	            (
   241	                f"nohup osascript -e {shlex.quote(script)} "
   242	                f">{shlex.quote(dialog_log)} 2>&1 & sleep 1"
   243	            ),
   244	        )
   245	
   246	    def _build_transfer_service(self, running_vm: RunningVm) -> TransferService:
   247	        return TransferService(running_vm)
   248	
   249	    def _build_talon_client(
   250	        self, running_vm: RunningVm, transfer_service: TransferService
   251	    ) -> TalonClient:
   252	        return TalonClient(running_vm, transfer_service)
   253	
   254	    def verify_screenshots_differ(self, before_path: Path, after_path: Path) -> None:
   255	        if before_path.read_bytes() == after_path.read_bytes():
   256	            raise click.ClickException(
   257	                "Smoke test screenshots did not change after the guest visual change."
   258	            )
   259	
   260	    def run_step(
   261	        self,
   262	        name: str,
   263	        action: Callable[[], object],
   264	        *,
   265	        success_message: str | None = None,
   266	    ) -> object:
   267	        self.log("STEP", name)
   268	        try:
   269	            result = action()
   270	        except click.ClickException as error:
   271	            self._fail(f"{name}: {error.message}")
   272	        except click.exceptions.Exit as error:
   273	            exit_code = getattr(error, "exit_code", 1)
   274	            self._fail(f"{name}: command exited with status {exit_code}")
   275	        except Exception as error:
   276	            self._fail(f"{name}: {error}")
   277	        else:
   278	            self.log("PASS", success_message or name)
   279	            return result
   280	
   281	    def log(self, status: str, message: str | Path) -> None:
   282	        click.echo(f"{status} {message}")
   283	
   284	    def _fail(self, message: str, *, screenshot_path: Path | None = None) -> None:
   285	        if screenshot_path is None and self._hint_screenshot is not None:
   286	            screenshot_path = self._hint_screenshot()
   287	        self.log("FAIL", message)
   288	        self._print_hints(screenshot_path=screenshot_path)
   289	        raise click.exceptions.Exit(1)
   290	
   291	    def _print_hints(self, *, screenshot_path: Path | None) -> None:
   292	        click.echo("HINT rerun with --debug for command traces and transport details.")
   293	        if self.vm_controller.debug:
   294	            click.echo(
   295	                "HINT --debug is already enabled; inspect the command trace above."
   296	            )
   297	        click.echo(
   298	            "HINT inspect guest logs at ~/.talon/talon.log and /tmp/talonbox-talon.log."
   299	        )
   300	        if screenshot_path is not None:
   301	            click.echo(f"HINT inspect the saved screenshot at {screenshot_path}.")
```

The second half focuses on diagnostics and operator experience. `validate_screenshot()` does a basic but useful PNG signature check. `trigger_visual_change()` creates a guest-side dialog so the second screenshot should differ from the first. `run_step()` standardizes logging and error conversion so every phase reports `STEP`, `PASS`, or `FAIL` in the same style. Finally, `_print_hints()` points the operator toward `--debug`, the Talon log files, and any saved screenshot artifact.

That combination makes `smoke-test` the most opinionated command in the tool, but also the most revealing. It shows the order in which the author expects real work to happen and it encodes the debugging breadcrumbs that matter when any one of those layers fails.

## 8. The Test Suite Serves as an Executable Specification

The repository has a single large test module, `tests/test_talonbox.py`, but it is organized by behavior rather than by raw source-file order. That makes it especially useful for understanding intent, because each cluster of tests tells you what contract a module is supposed to preserve.

```bash
rg -n '^def test_' tests/test_talonbox.py | sed -n '1,120p'
```

```output
64:def test_version() -> None:
73:def test_root_help_groups_commands_and_examples() -> None:
91:def test_exec_help_explains_double_dash_usage() -> None:
101:def test_mimic_help_works() -> None:
112:def test_smoke_test_help_mentions_artifacts_and_confirmation() -> None:
125:def test_start_command_delegates_to_vm_controller(
146:def test_show_command_delegates_to_vm_controller(
167:def test_smoke_test_command_passes_yes_to_runner(
188:def test_cli_rejects_non_macos_before_running_commands(
209:def test_repl_reads_stdin_when_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
227:def test_vm_controller_format_vm_info_includes_vnc() -> None:
243:def test_vm_controller_start_boots_vm_and_restarts_talon(
294:def test_vm_controller_start_cleans_up_failed_launch(
345:def test_running_vm_restart_talon_waits_for_repl_and_sleeps(
377:def test_vm_controller_stop_falls_back_to_force_stop_for_stuck_vm(
430:def test_write_smoke_test_bundle_includes_action_docstring(tmp_path: Path) -> None:
444:def test_trigger_smoke_test_visual_change_uses_guest_dialog(
472:def test_verify_smoke_test_screenshots_differ_rejects_identical_files(
486:def test_smoke_test_runner_cancellation_leaves_running_vm_untouched(
513:def test_smoke_test_runner_success_runs_end_to_end(
608:def test_smoke_test_runner_failure_after_start_still_stops_vm(
659:def test_smoke_test_runner_rejects_invalid_screenshot(
722:def test_transfer_service_rsync_rewrites_guest_destination() -> None:
736:def test_transfer_service_scp_download_rewrites_guest_source() -> None:
747:def test_transfer_service_rejects_transport_override() -> None:
756:def test_transfer_service_allows_rsync_host_write_flag_inside_sandbox() -> None:
770:def test_transfer_service_rejects_guest_to_guest() -> None:
777:def test_transfer_service_rejects_local_to_local() -> None:
788:def test_transfer_service_rejects_symlink_escape_from_tmp(
807:def test_exec_command_runs_guest_shell_and_propagates_exit_code(
836:def test_talon_client_repl_waits_for_socket_then_runs_script(
873:def test_talon_client_mimic_uses_python_escaped_payload(
901:def test_talon_client_screenshot_uses_talon_capture_and_download(
955:def test_talon_client_screenshot_rejects_output_outside_tmp(
966:def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
982:def test_get_vm_info_tolerates_log_line_before_json(
1007:def test_get_vm_info_reads_vnc_url(monkeypatch: pytest.MonkeyPatch) -> None:
1032:def test_wait_for_running_vm_reports_launch_log_when_lume_run_exits_early(
1058:def test_transfer_service_rsync_uses_fixed_vm_shell(
1095:def test_transfer_service_scp_uses_fixed_vm_ssh_options(
1150:def test_transfer_service_sandbox_profile_allows_tmp_and_dev(
1168:def test_running_vm_download_uses_scp(monkeypatch: pytest.MonkeyPatch) -> None:
1219:def test_running_vm_run_repl_retries_transient_ssh_failure(
1249:def test_running_vm_download_retries_transient_ssh_failure(
1278:def test_running_vm_wait_for_talon_repl_checks_socket_path(
```

At a high level, the tests cover five themes: CLI help and delegation, VM lifecycle behavior, smoke-test orchestration, transfer safety, and the lower-level transport/parsing details in `lume.py`, `vm.py`, and `talon_client.py`. Reading the list of test names is enough to see where the authors consider regressions likely or expensive.

The first cluster exercises the public surface. These tests confirm that help text stays descriptive, the macOS guard runs before command execution, stdin-backed REPL usage works, and `VmController.start()` and `stop()` perform the expected sequence of operations.

```bash
nl -ba tests/test_talonbox.py | sed -n '64,430p'
```

```output
    64	def test_version() -> None:
    65	    runner = CliRunner()
    66	
    67	    result = runner.invoke(cli, ["--version"])
    68	
    69	    assert result.exit_code == 0
    70	    assert result.output.startswith("talonbox, version ")
    71	
    72	
    73	def test_root_help_groups_commands_and_examples() -> None:
    74	    runner = CliRunner()
    75	
    76	    result = runner.invoke(cli, ["--help"])
    77	
    78	    assert result.exit_code == 0
    79	    assert "Minimal Talon VM control primitives for coding agents." in result.output
    80	    assert "Use `show` for a read-only status check" in result.output
    81	    assert "VM lifecycle:" in result.output
    82	    assert "Guest shell:" in result.output
    83	    assert "Talon RPC:" in result.output
    84	    assert "scp" in result.output
    85	    assert "restart-talon" in result.output
    86	    assert "smoke-test" in result.output
    87	    assert "talonbox exec -- uname -a" in result.output
    88	    assert "talonbox smoke-test" in result.output
    89	
    90	
    91	def test_exec_help_explains_double_dash_usage() -> None:
    92	    runner = CliRunner()
    93	
    94	    result = runner.invoke(cli, ["exec", "--help"])
    95	
    96	    assert result.exit_code == 0
    97	    assert "Place `--` before the remote command" in result.output
    98	    assert "talonbox exec -- whoami" in result.output
    99	
   100	
   101	def test_mimic_help_works() -> None:
   102	    runner = CliRunner()
   103	
   104	    result = runner.invoke(cli, ["mimic", "--help"])
   105	
   106	    assert result.exit_code == 0
   107	    assert (
   108	        "Send one phrase to the guest Talon REPL as `mimic(<phrase>)`." in result.output
   109	    )
   110	
   111	
   112	def test_smoke_test_help_mentions_artifacts_and_confirmation() -> None:
   113	    runner = CliRunner()
   114	
   115	    result = runner.invoke(cli, ["smoke-test", "--help"])
   116	
   117	    assert result.exit_code == 0
   118	    assert "Run a mutating end-to-end sanity check" in result.output
   119	    assert "may stop a running VM" in result.output
   120	    assert "Artifacts are kept under `/tmp`" in result.output
   121	    assert "left stopped" in result.output
   122	    assert "talonbox smoke-test --yes" in result.output
   123	
   124	
   125	def test_start_command_delegates_to_vm_controller(
   126	    monkeypatch: pytest.MonkeyPatch,
   127	) -> None:
   128	    runner = CliRunner()
   129	    monkeypatch.setattr(
   130	        cli_module.VmController,
   131	        "start",
   132	        lambda self: _running_vm(),
   133	    )
   134	    monkeypatch.setattr(
   135	        cli_module.VmController,
   136	        "format_vm_info",
   137	        lambda self, info: ["status: running", "ip: 192.168.64.10"],
   138	    )
   139	
   140	    result = runner.invoke(cli, ["start"])
   141	
   142	    assert result.exit_code == 0
   143	    assert result.output == "status: running\nip: 192.168.64.10\n"
   144	
   145	
   146	def test_show_command_delegates_to_vm_controller(
   147	    monkeypatch: pytest.MonkeyPatch,
   148	) -> None:
   149	    runner = CliRunner()
   150	    monkeypatch.setattr(
   151	        cli_module.VmController,
   152	        "get_vm",
   153	        lambda self: VmInfo(self.vm, "running", "192.168.64.10"),
   154	    )
   155	    monkeypatch.setattr(
   156	        cli_module.VmController,
   157	        "format_vm_info",
   158	        lambda self, info: ["status: running", "ip: 192.168.64.10"],
   159	    )
   160	
   161	    result = runner.invoke(cli, ["show"])
   162	
   163	    assert result.exit_code == 0
   164	    assert result.output == "status: running\nip: 192.168.64.10\n"
   165	
   166	
   167	def test_smoke_test_command_passes_yes_to_runner(
   168	    monkeypatch: pytest.MonkeyPatch,
   169	) -> None:
   170	    runner = CliRunner()
   171	    calls: list[bool] = []
   172	
   173	    class FakeRunner:
   174	        def run(self, *, yes: bool, confirm: object = click.confirm) -> None:
   175	            del confirm
   176	            calls.append(yes)
   177	
   178	    monkeypatch.setattr(
   179	        cli_module, "_build_smoke_test_runner", lambda settings: FakeRunner()
   180	    )
   181	
   182	    result = runner.invoke(cli, ["smoke-test", "--yes"])
   183	
   184	    assert result.exit_code == 0
   185	    assert calls == [True]
   186	
   187	
   188	def test_cli_rejects_non_macos_before_running_commands(
   189	    monkeypatch: pytest.MonkeyPatch,
   190	) -> None:
   191	    runner = CliRunner()
   192	    calls: list[str] = []
   193	
   194	    monkeypatch.setattr(cli_module.sys, "platform", "linux")
   195	    monkeypatch.setattr(
   196	        cli_module.VmController,
   197	        "get_vm",
   198	        lambda self: calls.append("get_vm")
   199	        or VmInfo(self.vm, "running", "192.168.64.10"),
   200	    )
   201	
   202	    result = runner.invoke(cli, ["show"])
   203	
   204	    assert result.exit_code == 1
   205	    assert "supports only macOS hosts" in result.output
   206	    assert calls == []
   207	
   208	
   209	def test_repl_reads_stdin_when_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
   210	    runner = CliRunner()
   211	    payloads: list[str] = []
   212	
   213	    class FakeClient:
   214	        def repl(self, code: str) -> None:
   215	            payloads.append(code)
   216	
   217	    monkeypatch.setattr(
   218	        cli_module, "_build_talon_client", lambda settings: FakeClient()
   219	    )
   220	
   221	    result = runner.invoke(cli, ["repl"], input="print(1)\n")
   222	
   223	    assert result.exit_code == 0
   224	    assert payloads == ["print(1)\n"]
   225	
   226	
   227	def test_vm_controller_format_vm_info_includes_vnc() -> None:
   228	    vm_controller = VmController("talon-test", False)
   229	
   230	    lines = vm_controller.format_vm_info(
   231	        VmInfo("talon-test", "running", "192.168.64.10", "vnc://127.0.0.1:5901")
   232	    )
   233	
   234	    assert lines == [
   235	        "status: running",
   236	        "ip: 192.168.64.10",
   237	        "username: lume",
   238	        "password: lume",
   239	        "vnc: vnc://127.0.0.1:5901",
   240	    ]
   241	
   242	
   243	def test_vm_controller_start_boots_vm_and_restarts_talon(
   244	    monkeypatch: pytest.MonkeyPatch,
   245	) -> None:
   246	    vm_controller = VmController("talon-test", False)
   247	    probe_calls: list[float] = []
   248	    restart_calls: list[tuple[bool, bool]] = []
   249	    running_vm = _running_vm()
   250	
   251	    _set_vm_statuses(monkeypatch, ("stopped", None))
   252	    monkeypatch.setattr(
   253	        vm_module.lume,
   254	        "spawn_vm",
   255	        lambda vm, debug=False: _fake_launch(),
   256	    )
   257	    monkeypatch.setattr(
   258	        vm_module.lume,
   259	        "wait_for_running_vm",
   260	        lambda vm, timeout, debug=False, launch=None: VmInfo(
   261	            vm, "running", "192.168.64.10"
   262	        ),
   263	    )
   264	    monkeypatch.setattr(
   265	        vm_controller,
   266	        "_running_vm_from_info",
   267	        lambda info: running_vm,
   268	    )
   269	    monkeypatch.setattr(
   270	        running_vm,
   271	        "probe_ssh",
   272	        lambda *, timeout=0: probe_calls.append(timeout),
   273	    )
   274	    monkeypatch.setattr(
   275	        running_vm,
   276	        "restart_talon",
   277	        lambda *, wipe_user_dir, clean_logs: restart_calls.append(
   278	            (wipe_user_dir, clean_logs)
   279	        ),
   280	    )
   281	    monkeypatch.setattr(
   282	        vm_module.lume,
   283	        "cleanup_launch_log",
   284	        lambda log_path: None,
   285	    )
   286	
   287	    info = vm_controller.start()
   288	
   289	    assert info is running_vm
   290	    assert probe_calls == [vm_module.SSH_TIMEOUT_SECONDS]
   291	    assert restart_calls == [(True, True)]
   292	
   293	
   294	def test_vm_controller_start_cleans_up_failed_launch(
   295	    monkeypatch: pytest.MonkeyPatch,
   296	) -> None:
   297	    vm_controller = VmController("talon-test", True)
   298	    calls: list[object] = []
   299	    running_vm = _running_vm(debug=True)
   300	
   301	    _set_vm_statuses(monkeypatch, ("stopped", None))
   302	    monkeypatch.setattr(
   303	        vm_module.lume,
   304	        "spawn_vm",
   305	        lambda vm, debug=False: _fake_launch(),
   306	    )
   307	    monkeypatch.setattr(
   308	        vm_module.lume,
   309	        "wait_for_running_vm",
   310	        lambda vm, timeout, debug=False, launch=None: VmInfo(
   311	            vm, "running", "192.168.64.10"
   312	        ),
   313	    )
   314	
   315	    monkeypatch.setattr(
   316	        vm_controller,
   317	        "_running_vm_from_info",
   318	        lambda info: running_vm,
   319	    )
   320	
   321	    def fail_probe(*, timeout: float = 0.0) -> None:
   322	        del timeout
   323	        raise vm_module.TransportError("ssh failed: 192.168.64.10")
   324	
   325	    monkeypatch.setattr(running_vm, "probe_ssh", fail_probe)
   326	    monkeypatch.setattr(
   327	        vm_module.lume,
   328	        "stop_vm",
   329	        lambda vm, debug=False: calls.append(("stop_vm", vm)),
   330	    )
   331	    monkeypatch.setattr(
   332	        vm_module.lume,
   333	        "wait_for_status",
   334	        lambda vm, status, timeout, debug=False: (
   335	            calls.append(("wait_for_status", timeout)) or VmInfo(vm, "stopped", None)
   336	        ),
   337	    )
   338	
   339	    with pytest.raises(click.ClickException, match="ssh failed: 192.168.64.10"):
   340	        vm_controller.start()
   341	
   342	    assert calls == [("stop_vm", "talon-test"), ("wait_for_status", 30.0)]
   343	
   344	
   345	def test_running_vm_restart_talon_waits_for_repl_and_sleeps(
   346	    monkeypatch: pytest.MonkeyPatch,
   347	) -> None:
   348	    running_vm = _running_vm()
   349	    calls: list[tuple[str, object]] = []
   350	    sleeps: list[float] = []
   351	
   352	    monkeypatch.setattr(
   353	        running_vm,
   354	        "run_shell",
   355	        lambda command, **kwargs: (
   356	            calls.append((running_vm.ip_address, command))
   357	            or subprocess.CompletedProcess([], 0, "", "")
   358	        ),
   359	    )
   360	    monkeypatch.setattr(
   361	        running_vm,
   362	        "wait_for_talon_repl",
   363	        lambda **kwargs: calls.append((running_vm.ip_address, "wait_for_talon_repl")),
   364	    )
   365	    monkeypatch.setattr(vm_module.time, "sleep", lambda seconds: sleeps.append(seconds))
   366	
   367	    running_vm.restart_talon(
   368	        wipe_user_dir=True,
   369	        clean_logs=True,
   370	    )
   371	
   372	    assert calls[0] == ("192.168.64.10", "pkill -x Talon >/dev/null 2>&1 || true")
   373	    assert calls[-1] == ("192.168.64.10", "wait_for_talon_repl")
   374	    assert sleeps == [vm_module.TALON_POST_RESTART_SETTLE_SECONDS]
   375	
   376	
   377	def test_vm_controller_stop_falls_back_to_force_stop_for_stuck_vm(
   378	    monkeypatch: pytest.MonkeyPatch,
   379	) -> None:
   380	    vm_controller = VmController("talon-test", False)
   381	    calls: list[tuple[str, object]] = []
   382	    running_vm = _running_vm()
   383	
   384	    monkeypatch.setattr(
   385	        vm_module.lume,
   386	        "get_vm_info",
   387	        lambda vm, debug=False: VmInfo(vm, "running", "192.168.64.10"),
   388	    )
   389	    monkeypatch.setattr(vm_controller, "_running_vm_from_info", lambda info: running_vm)
   390	    monkeypatch.setattr(
   391	        running_vm,
   392	        "logout_guest_session",
   393	        lambda: calls.append(("logout_guest_session", running_vm.ip_address)),
   394	    )
   395	    monkeypatch.setattr(
   396	        vm_module.lume,
   397	        "stop_vm",
   398	        lambda vm, debug=False: calls.append(("stop_vm", vm)),
   399	    )
   400	
   401	    def fake_wait_for_status(
   402	        vm: str, status: str, timeout: float, debug: bool = False
   403	    ) -> VmInfo:
   404	        del status, debug
   405	        calls.append(("wait_for_status", timeout))
   406	        if timeout == 60.0:
   407	            raise lume_module.LumeError(
   408	                "Timed out waiting for VM to reach status stopped: talon-test"
   409	            )
   410	        return VmInfo(vm, "stopped", None)
   411	
   412	    monkeypatch.setattr(vm_module.lume, "wait_for_status", fake_wait_for_status)
   413	    monkeypatch.setattr(
   414	        vm_module.lume,
   415	        "force_stop_vm",
   416	        lambda vm, debug=False: calls.append(("force_stop_vm", vm)),
   417	    )
   418	
   419	    vm_controller.stop()
   420	
   421	    assert calls == [
   422	        ("logout_guest_session", "192.168.64.10"),
   423	        ("stop_vm", "talon-test"),
   424	        ("wait_for_status", 60.0),
   425	        ("force_stop_vm", "talon-test"),
   426	        ("wait_for_status", 20.0),
   427	    ]
   428	
   429	
   430	def test_write_smoke_test_bundle_includes_action_docstring(tmp_path: Path) -> None:
```

The next cluster is the heart of the end-to-end story. It checks the smoke-test bundle contents, confirms the visual-change dialog command, verifies that successful runs execute the right high-level steps in order, and makes sure failure cases still stop the VM and emit the right hints. That is exactly the kind of coverage you want for a command that intentionally mutates state across several subsystems.

```bash
nl -ba tests/test_talonbox.py | sed -n '430,722p'
```

```output
   430	def test_write_smoke_test_bundle_includes_action_docstring(tmp_path: Path) -> None:
   431	    vm_controller, _, _ = _build_service_stack()
   432	    runner = SmokeTestRunner(vm_controller)
   433	
   434	    runner.write_bundle(tmp_path, "/tmp/marker.txt", "token")
   435	
   436	    assert "user.talonbox_smoke_test()" in (
   437	        tmp_path / "talonbox_smoke_test.talon"
   438	    ).read_text(encoding="utf-8")
   439	    assert '"""Write the talonbox smoke-test marker file."""' in (
   440	        tmp_path / "talonbox_smoke_test.py"
   441	    ).read_text(encoding="utf-8")
   442	
   443	
   444	def test_trigger_smoke_test_visual_change_uses_guest_dialog(
   445	    monkeypatch: pytest.MonkeyPatch,
   446	) -> None:
   447	    vm_controller, _, _ = _build_service_stack()
   448	    runner = SmokeTestRunner(vm_controller)
   449	    calls: list[tuple[str, str]] = []
   450	    running_vm = _running_vm()
   451	    monkeypatch.setattr(
   452	        running_vm,
   453	        "run_shell",
   454	        lambda command, **kwargs: (
   455	            calls.append((running_vm.ip_address, command))
   456	            or subprocess.CompletedProcess([], 0, "", "")
   457	        ),
   458	    )
   459	
   460	    runner.trigger_visual_change(running_vm, "abc123")
   461	
   462	    assert calls == [
   463	        (
   464	            "192.168.64.10",
   465	            'nohup osascript -e \'display dialog "talonbox screenshot test abc123" '
   466	            'buttons {"OK"} default button 1 giving up after 15\' '
   467	            ">/tmp/talonbox-smoke-test-dialog-abc123.log 2>&1 & sleep 1",
   468	        )
   469	    ]
   470	
   471	
   472	def test_verify_smoke_test_screenshots_differ_rejects_identical_files(
   473	    tmp_path: Path,
   474	) -> None:
   475	    vm_controller, _, _ = _build_service_stack()
   476	    runner = SmokeTestRunner(vm_controller)
   477	    before = tmp_path / "before.png"
   478	    after = tmp_path / "after.png"
   479	    before.write_bytes(b"same")
   480	    after.write_bytes(b"same")
   481	
   482	    with pytest.raises(click.ClickException, match="did not change"):
   483	        runner.verify_screenshots_differ(before, after)
   484	
   485	
   486	def test_smoke_test_runner_cancellation_leaves_running_vm_untouched(
   487	    monkeypatch: pytest.MonkeyPatch,
   488	    capsys: pytest.CaptureFixture[str],
   489	) -> None:
   490	    vm_controller, _, _ = _build_service_stack()
   491	    runner = SmokeTestRunner(vm_controller)
   492	
   493	    monkeypatch.setattr(
   494	        vm_controller,
   495	        "get_vm",
   496	        lambda: VmInfo("talon-test", "running", "192.168.64.10"),
   497	    )
   498	    monkeypatch.setattr(
   499	        vm_controller,
   500	        "stop",
   501	        lambda: pytest.fail("stop should not be called"),
   502	    )
   503	
   504	    with pytest.raises(click.exceptions.Exit) as error:
   505	        runner.run(yes=False, confirm=lambda prompt, default=False: False)
   506	
   507	    captured = capsys.readouterr()
   508	    assert error.value.exit_code == 1
   509	    assert "VM talon-test is already running." in captured.out
   510	    assert "FAIL smoke-test canceled by user; VM left running." in captured.out
   511	
   512	
   513	def test_smoke_test_runner_success_runs_end_to_end(
   514	    monkeypatch: pytest.MonkeyPatch,
   515	    tmp_path: Path,
   516	    capsys: pytest.CaptureFixture[str],
   517	) -> None:
   518	    vm_controller, _, _ = _build_service_stack()
   519	    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
   520	    steps: list[str] = []
   521	    running_vm = _running_vm()
   522	    transfer_service = TransferService(running_vm)
   523	
   524	    states = [VmInfo("talon-test", "stopped", None)]
   525	    monkeypatch.setattr(
   526	        vm_controller,
   527	        "get_vm",
   528	        lambda: states[0],
   529	    )
   530	    monkeypatch.setattr(
   531	        vm_controller,
   532	        "start",
   533	        lambda: steps.append("start") or running_vm,
   534	    )
   535	    monkeypatch.setattr(
   536	        runner,
   537	        "_build_transfer_service",
   538	        lambda running_vm_arg: transfer_service,
   539	    )
   540	    monkeypatch.setattr(
   541	        transfer_service,
   542	        "rsync",
   543	        lambda args: steps.append("rsync") or 0,
   544	    )
   545	    monkeypatch.setattr(
   546	        vm_controller,
   547	        "restart_talon",
   548	        lambda *, wipe_user_dir, clean_logs: steps.append(
   549	            f"restart:{wipe_user_dir}:{clean_logs}"
   550	        ),
   551	    )
   552	
   553	    class FakeClient:
   554	        def mimic(self, command: str) -> None:
   555	            steps.append(f"mimic:{command}")
   556	
   557	        def capture_screenshot(self, path: Path) -> None:
   558	            steps.append(f"capture:{path.name}")
   559	            path.write_bytes(b"\x89PNG\r\n\x1a\npayload")
   560	
   561	    monkeypatch.setattr(
   562	        runner,
   563	        "_build_talon_client",
   564	        lambda running_vm_arg, transfer_service_arg: FakeClient(),
   565	    )
   566	    monkeypatch.setattr(
   567	        runner,
   568	        "verify_marker",
   569	        lambda running_vm_arg, marker_path, token: steps.append("verify_marker"),
   570	    )
   571	    monkeypatch.setattr(
   572	        runner,
   573	        "trigger_visual_change",
   574	        lambda running_vm_arg, token: steps.append("show_dialog"),
   575	    )
   576	    monkeypatch.setattr(
   577	        runner,
   578	        "verify_screenshots_differ",
   579	        lambda before, after: steps.append("verify_diff"),
   580	    )
   581	    monkeypatch.setattr(
   582	        vm_controller,
   583	        "stop",
   584	        lambda: steps.append("stop"),
   585	    )
   586	
   587	    runner.run(yes=False)
   588	
   589	    captured = capsys.readouterr()
   590	    assert "ARTIFACT " in captured.out
   591	    assert "PASS Smoke test completed successfully." in captured.out
   592	    assert steps == [
   593	        "start",
   594	        "rsync",
   595	        "restart:False:True",
   596	        "mimic:talonbox smoke test",
   597	        "verify_marker",
   598	        "capture:screenshot-before-dialog.png",
   599	        "show_dialog",
   600	        "capture:screenshot-after-dialog.png",
   601	        "verify_diff",
   602	        "stop",
   603	    ]
   604	    artifact_dir = next(tmp_path.iterdir())
   605	    assert (artifact_dir / "bundle" / "talonbox_smoke_test.talon").exists()
   606	
   607	
   608	def test_smoke_test_runner_failure_after_start_still_stops_vm(
   609	    monkeypatch: pytest.MonkeyPatch,
   610	    tmp_path: Path,
   611	    capsys: pytest.CaptureFixture[str],
   612	) -> None:
   613	    vm_controller, _, _ = _build_service_stack()
   614	    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
   615	    stop_calls: list[str] = []
   616	    transfer_service = TransferService(_running_vm())
   617	
   618	    monkeypatch.setattr(
   619	        vm_controller,
   620	        "get_vm",
   621	        lambda: VmInfo("talon-test", "stopped", None),
   622	    )
   623	    monkeypatch.setattr(
   624	        vm_controller,
   625	        "start",
   626	        lambda: _running_vm(),
   627	    )
   628	    monkeypatch.setattr(
   629	        runner,
   630	        "_build_transfer_service",
   631	        lambda running_vm_arg: transfer_service,
   632	    )
   633	    monkeypatch.setattr(transfer_service, "rsync", lambda args: 0)
   634	    monkeypatch.setattr(
   635	        vm_controller,
   636	        "restart_talon",
   637	        lambda *, wipe_user_dir, clean_logs: (_ for _ in ()).throw(
   638	            click.ClickException("talon restart failed")
   639	        ),
   640	    )
   641	    monkeypatch.setattr(vm_controller, "stop", lambda: stop_calls.append("stop"))
   642	
   643	    with pytest.raises(click.exceptions.Exit) as error:
   644	        runner.run(yes=False)
   645	
   646	    captured = capsys.readouterr()
   647	    assert error.value.exit_code == 1
   648	    assert (
   649	        "FAIL Restart Talon to load the uploaded bundle: talon restart failed"
   650	        in captured.out
   651	    )
   652	    assert (
   653	        "HINT inspect guest logs at ~/.talon/talon.log and /tmp/talonbox-talon.log."
   654	        in captured.out
   655	    )
   656	    assert stop_calls == ["stop"]
   657	
   658	
   659	def test_smoke_test_runner_rejects_invalid_screenshot(
   660	    monkeypatch: pytest.MonkeyPatch,
   661	    tmp_path: Path,
   662	    capsys: pytest.CaptureFixture[str],
   663	) -> None:
   664	    vm_controller, _, _ = _build_service_stack()
   665	    runner = SmokeTestRunner(vm_controller, host_output_root=tmp_path.resolve())
   666	    stop_calls: list[str] = []
   667	    running_vm = _running_vm()
   668	    transfer_service = TransferService(running_vm)
   669	
   670	    monkeypatch.setattr(
   671	        vm_controller,
   672	        "get_vm",
   673	        lambda: VmInfo("talon-test", "stopped", None),
   674	    )
   675	    monkeypatch.setattr(
   676	        vm_controller,
   677	        "start",
   678	        lambda: running_vm,
   679	    )
   680	    monkeypatch.setattr(
   681	        runner,
   682	        "_build_transfer_service",
   683	        lambda running_vm_arg: transfer_service,
   684	    )
   685	    monkeypatch.setattr(transfer_service, "rsync", lambda args: 0)
   686	    monkeypatch.setattr(
   687	        vm_controller,
   688	        "restart_talon",
   689	        lambda *, wipe_user_dir, clean_logs: None,
   690	    )
   691	
   692	    class FakeClient:
   693	        def mimic(self, command: str) -> None:
   694	            return None
   695	
   696	        def capture_screenshot(self, path: Path) -> None:
   697	            path.write_bytes(b"not-a-png")
   698	
   699	    monkeypatch.setattr(
   700	        runner,
   701	        "_build_talon_client",
   702	        lambda running_vm_arg, transfer_service_arg: FakeClient(),
   703	    )
   704	    monkeypatch.setattr(
   705	        runner, "verify_marker", lambda running_vm_arg, marker_path, token: None
   706	    )
   707	    monkeypatch.setattr(vm_controller, "stop", lambda: stop_calls.append("stop"))
   708	
   709	    with pytest.raises(click.exceptions.Exit) as error:
   710	        runner.run(yes=False)
   711	
   712	    captured = capsys.readouterr()
   713	    assert error.value.exit_code == 1
   714	    assert (
   715	        "FAIL Validate the baseline screenshot artifact: Smoke test screenshot was not a PNG file"
   716	        in captured.out
   717	    )
   718	    assert "HINT inspect the saved screenshot at" in captured.out
   719	    assert stop_calls == ["stop"]
   720	
   721	
   722	def test_transfer_service_rsync_rewrites_guest_destination() -> None:
```

After that, the tests turn back to safety and RPC behavior. They check guest-path rewriting for `rsync` and `scp`, rejection of dangerous transfer options and path combinations, screenshot output restrictions, mimic/repl payload formatting, and screenshot download cleanup. These tests are effectively the proof that the host-write boundary is not just documented but enforced.

```bash
nl -ba tests/test_talonbox.py | sed -n '722,966p'
```

```output
   722	def test_transfer_service_rsync_rewrites_guest_destination() -> None:
   723	    _, transfer_service, _ = _build_service_stack()
   724	
   725	    args = transfer_service.prepare_rsync_args(
   726	        ["-av", "./repo/", "guest:/Users/lume/.talon/user/repo/"]
   727	    )
   728	
   729	    assert args == [
   730	        "-av",
   731	        "./repo/",
   732	        "lume@192.168.64.10:/Users/lume/.talon/user/repo/",
   733	    ]
   734	
   735	
   736	def test_transfer_service_scp_download_rewrites_guest_source() -> None:
   737	    _, transfer_service, _ = _build_service_stack()
   738	
   739	    args = transfer_service.prepare_scp_args(["guest:/tmp/out.png", "/tmp/out.png"])
   740	
   741	    assert args == [
   742	        "lume@192.168.64.10:/tmp/out.png",
   743	        str(Path("/tmp/out.png").resolve(strict=False)),
   744	    ]
   745	
   746	
   747	def test_transfer_service_rejects_transport_override() -> None:
   748	    _, transfer_service, _ = _build_service_stack()
   749	
   750	    with pytest.raises(click.ClickException, match="Option not allowed"):
   751	        transfer_service.prepare_rsync_args(
   752	            ["-e", "ssh", "./repo/", "guest:/tmp/repo/"]
   753	        )
   754	
   755	
   756	def test_transfer_service_allows_rsync_host_write_flag_inside_sandbox() -> None:
   757	    _, transfer_service, _ = _build_service_stack()
   758	
   759	    args = transfer_service.prepare_rsync_args(
   760	        ["--log-file=/tmp/talonbox-rsync.log", "./repo/", "guest:/tmp/repo/"]
   761	    )
   762	
   763	    assert args == [
   764	        "--log-file=/tmp/talonbox-rsync.log",
   765	        "./repo/",
   766	        "lume@192.168.64.10:/tmp/repo/",
   767	    ]
   768	
   769	
   770	def test_transfer_service_rejects_guest_to_guest() -> None:
   771	    _, transfer_service, _ = _build_service_stack()
   772	
   773	    with pytest.raises(click.ClickException, match="Guest-to-guest"):
   774	        transfer_service.prepare_scp_args(["guest:/tmp/a", "guest:/tmp/b"])
   775	
   776	
   777	def test_transfer_service_rejects_local_to_local() -> None:
   778	    _, transfer_service, _ = _build_service_stack()
   779	
   780	    with pytest.raises(
   781	        click.ClickException, match="Local-to-local transfers are not allowed"
   782	    ):
   783	        transfer_service.prepare_rsync_args(
   784	            ["-av", "./repo/", "/Users/lume/.talon/user/repo/"]
   785	        )
   786	
   787	
   788	def test_transfer_service_rejects_symlink_escape_from_tmp(
   789	    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   790	) -> None:
   791	    _, transfer_service, _ = _build_service_stack()
   792	    escape_root = tmp_path.resolve()
   793	    outside_dir = tmp_path.parent / "outside"
   794	    outside_dir.mkdir()
   795	    (escape_root / "link").symlink_to(outside_dir, target_is_directory=True)
   796	
   797	    monkeypatch.setattr(transfer_service, "_host_output_root", lambda: escape_root)
   798	
   799	    with pytest.raises(
   800	        click.ClickException, match="Symlinks that escape /tmp are not allowed."
   801	    ):
   802	        transfer_service.prepare_rsync_args(
   803	            ["-av", "guest:/tmp/out.txt", str(escape_root / "link" / "out.txt")]
   804	        )
   805	
   806	
   807	def test_exec_command_runs_guest_shell_and_propagates_exit_code(
   808	    monkeypatch: pytest.MonkeyPatch,
   809	) -> None:
   810	    runner = CliRunner()
   811	    running_vm = _running_vm()
   812	    calls: list[tuple[str, list[str]]] = []
   813	
   814	    monkeypatch.setattr(
   815	        cli_module.VmController, "get_running_vm", lambda self: running_vm
   816	    )
   817	
   818	    def fake_exec(
   819	        command_args: list[str],
   820	        stream: bool = False,
   821	        check: bool = True,
   822	    ) -> subprocess.CompletedProcess[str]:
   823	        calls.append((running_vm.ip_address, command_args))
   824	        assert stream is True
   825	        assert check is False
   826	        return subprocess.CompletedProcess([], 7, "", "")
   827	
   828	    monkeypatch.setattr(running_vm, "run_shell", fake_exec)
   829	
   830	    result = runner.invoke(cli, ["exec", "--", "echo", "hi"])
   831	
   832	    assert result.exit_code == 7
   833	    assert calls == [("192.168.64.10", ["echo", "hi"])]
   834	
   835	
   836	def test_talon_client_repl_waits_for_socket_then_runs_script(
   837	    monkeypatch: pytest.MonkeyPatch,
   838	) -> None:
   839	    vm_controller, transfer_service, talon_client = _build_service_stack()
   840	    waits: list[tuple[str, float]] = []
   841	    payloads: list[tuple[str, str, bool]] = []
   842	
   843	    monkeypatch.setattr(
   844	        talon_client.running_vm,
   845	        "wait_for_talon_repl",
   846	        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: waits.append(
   847	            (talon_client.running_vm.ip_address, timeout)
   848	        ),
   849	    )
   850	    monkeypatch.setattr(
   851	        talon_client.running_vm,
   852	        "run_repl",
   853	        lambda payload, stream_output=False: (
   854	            payloads.append(
   855	                (talon_client.running_vm.ip_address, payload, stream_output)
   856	            )
   857	            or subprocess.CompletedProcess([], 0, "", "")
   858	        ),
   859	    )
   860	
   861	    talon_client.repl("if True:\n    print(1)\nprint(2)\n")
   862	
   863	    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
   864	    assert payloads == [
   865	        (
   866	            "192.168.64.10",
   867	            "exec('if True:\\n    print(1)\\nprint(2)\\n')\n",
   868	            True,
   869	        )
   870	    ]
   871	
   872	
   873	def test_talon_client_mimic_uses_python_escaped_payload(
   874	    monkeypatch: pytest.MonkeyPatch,
   875	) -> None:
   876	    vm_controller, transfer_service, talon_client = _build_service_stack()
   877	    waits: list[tuple[str, float]] = []
   878	    payloads: list[str] = []
   879	
   880	    monkeypatch.setattr(
   881	        talon_client.running_vm,
   882	        "wait_for_talon_repl",
   883	        lambda *, timeout=vm_module.TALON_REPL_TIMEOUT_SECONDS: waits.append(
   884	            (talon_client.running_vm.ip_address, timeout)
   885	        ),
   886	    )
   887	    monkeypatch.setattr(
   888	        talon_client.running_vm,
   889	        "run_repl",
   890	        lambda payload, stream_output=False: (
   891	            payloads.append(payload) or subprocess.CompletedProcess([], 0, "", "")
   892	        ),
   893	    )
   894	
   895	    talon_client.mimic('say "hello"\nworld')
   896	
   897	    assert waits == [("192.168.64.10", vm_module.TALON_REPL_TIMEOUT_SECONDS)]
   898	    assert payloads == ["mimic('say \"hello\"\\nworld')\n"]
   899	
   900	
   901	def test_talon_client_screenshot_uses_talon_capture_and_download(
   902	    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   903	) -> None:
   904	    vm_controller, transfer_service, talon_client = _build_service_stack()
   905	    repl_payloads: list[str] = []
   906	    downloads: list[tuple[str, str, Path]] = []
   907	    cleanup_commands: list[str] = []
   908	    target = tmp_path / "shots" / "screen.png"
   909	
   910	    monkeypatch.setattr(
   911	        transfer_service, "_host_output_root", lambda: tmp_path.resolve()
   912	    )
   913	    monkeypatch.setattr(
   914	        talon_client.running_vm,
   915	        "wait_for_talon_repl",
   916	        lambda *, timeout=0: None,
   917	    )
   918	    monkeypatch.setattr(
   919	        talon_client.running_vm,
   920	        "run_repl",
   921	        lambda payload, stream_output=False: (
   922	            repl_payloads.append(payload) or subprocess.CompletedProcess([], 0, "", "")
   923	        ),
   924	    )
   925	    monkeypatch.setattr(
   926	        talon_client.running_vm,
   927	        "download",
   928	        lambda remote, local: (
   929	            downloads.append((talon_client.running_vm.ip_address, remote, local))
   930	            or local.write_bytes(b"not-a-png")
   931	        ),
   932	    )
   933	    monkeypatch.setattr(
   934	        talon_client.running_vm,
   935	        "run_shell",
   936	        lambda command, **kwargs: (
   937	            cleanup_commands.append(command)
   938	            or subprocess.CompletedProcess([], 0, "", "")
   939	        ),
   940	    )
   941	
   942	    talon_client.capture_screenshot(target)
   943	
   944	    assert target.parent.exists()
   945	    assert "screen.capture_rect(screen.main().rect, retina=False)" in repl_payloads[0]
   946	    assert (
   947	        "img.save(path) if hasattr(img, 'save') else img.write_file(path)"
   948	        in repl_payloads[0]
   949	    )
   950	    assert downloads[0][0] == "192.168.64.10"
   951	    assert downloads[0][2] == target
   952	    assert cleanup_commands[0].startswith('rm -f "/tmp/talonbox-screenshot-')
   953	
   954	
   955	def test_talon_client_screenshot_rejects_output_outside_tmp(
   956	    monkeypatch: pytest.MonkeyPatch,
   957	) -> None:
   958	    vm_controller, transfer_service, talon_client = _build_service_stack()
   959	
   960	    with pytest.raises(
   961	        click.ClickException, match="Local output paths must stay under /tmp"
   962	    ):
   963	        talon_client.capture_screenshot(Path("/Users/jwstout/Desktop/guest-screen.png"))
   964	
   965	
   966	def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
```

The final cluster reaches down into parser and transport edge cases. It proves that noisy `lume ls` output is tolerated, launch-log details are surfaced when startup fails, the generated rsync/scp commands embed the fixed SSH settings, the sandbox profile allows only the intended roots, and transient SSH failures trigger retries rather than immediate failure. These are the kinds of bugs that are painful to diagnose manually, so it is valuable that the repo locks them down with focused tests.

```bash
nl -ba tests/test_talonbox.py | sed -n '966,1295p'
```

```output
   966	def test_get_vm_info_surfaces_raw_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
   967	    monkeypatch.setattr(
   968	        lume_module,
   969	        "_run_lume",
   970	        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
   971	            args, 0, '{"bad"', ""
   972	        ),
   973	    )
   974	
   975	    with pytest.raises(
   976	        lume_module.LumeError,
   977	        match=r'Invalid JSON from `lume ls --format json`: \{"bad"',
   978	    ):
   979	        lume_module.get_vm_info("talon-test")
   980	
   981	
   982	def test_get_vm_info_tolerates_log_line_before_json(
   983	    monkeypatch: pytest.MonkeyPatch,
   984	) -> None:
   985	    noisy_output = """[2026-03-11T06:55:51Z] INFO: Cleaned up stale session file name=talon-test
   986	[
   987	  {
   988	    "name": "talon-test",
   989	    "status": "stopped",
   990	    "ipAddress": null
   991	  }
   992	]
   993	"""
   994	    monkeypatch.setattr(
   995	        lume_module,
   996	        "_run_lume",
   997	        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
   998	            args, 0, noisy_output, ""
   999	        ),
  1000	    )
  1001	
  1002	    info = lume_module.get_vm_info("talon-test")
  1003	
  1004	    assert info == VmInfo("talon-test", "stopped", None)
  1005	
  1006	
  1007	def test_get_vm_info_reads_vnc_url(monkeypatch: pytest.MonkeyPatch) -> None:
  1008	    output = """[
  1009	  {
  1010	    "name": "talon-test",
  1011	    "status": "running",
  1012	    "ipAddress": "192.168.64.10",
  1013	    "vncUrl": "vnc://127.0.0.1:5901"
  1014	  }
  1015	]
  1016	"""
  1017	    monkeypatch.setattr(
  1018	        lume_module,
  1019	        "_run_lume",
  1020	        lambda args, debug=False, capture_output=True: subprocess.CompletedProcess(
  1021	            args, 0, output, ""
  1022	        ),
  1023	    )
  1024	
  1025	    info = lume_module.get_vm_info("talon-test")
  1026	
  1027	    assert info == VmInfo(
  1028	        "talon-test", "running", "192.168.64.10", "vnc://127.0.0.1:5901"
  1029	    )
  1030	
  1031	
  1032	def test_wait_for_running_vm_reports_launch_log_when_lume_run_exits_early(
  1033	    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
  1034	) -> None:
  1035	    log_path = tmp_path / "lume-run.log"
  1036	    log_path.write_text("permission denied\nconfig.json\n", encoding="utf-8")
  1037	    launch = lume_module.VmLaunch(
  1038	        process=cast(
  1039	            subprocess.Popen[bytes], type("Process", (), {"poll": lambda self: 1})()
  1040	        ),
  1041	        log_path=log_path,
  1042	    )
  1043	    monkeypatch.setattr(
  1044	        lume_module,
  1045	        "get_vm_info",
  1046	        lambda name, debug=False: VmInfo(name, "stopped", None),
  1047	    )
  1048	
  1049	    with pytest.raises(lume_module.LumeError, match="permission denied"):
  1050	        lume_module.wait_for_running_vm(
  1051	            "talon-test",
  1052	            timeout=1.0,
  1053	            interval=0.0,
  1054	            launch=launch,
  1055	        )
  1056	
  1057	
  1058	def test_transfer_service_rsync_uses_fixed_vm_shell(
  1059	    monkeypatch: pytest.MonkeyPatch,
  1060	) -> None:
  1061	    recorded: list[list[str]] = []
  1062	    _, transfer_service, _ = _build_service_stack()
  1063	    monkeypatch.setattr(
  1064	        transfer_service,
  1065	        "_sandbox_command_prefix",
  1066	        lambda: ["sandbox-exec", "-p", "(profile)"],
  1067	    )
  1068	
  1069	    def fake_run(
  1070	        cmd: list[str], check: bool = False
  1071	    ) -> subprocess.CompletedProcess[bytes]:
  1072	        recorded.append(cmd)
  1073	        return subprocess.CompletedProcess(cmd, 0)
  1074	
  1075	    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)
  1076	
  1077	    returncode = transfer_service.rsync(["-av", "src/", "guest:/tmp/dest"])
  1078	
  1079	    assert returncode == 0
  1080	    assert recorded == [
  1081	        [
  1082	            "sandbox-exec",
  1083	            "-p",
  1084	            "(profile)",
  1085	            "rsync",
  1086	            "-e",
  1087	            "sshpass -p lume ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=no -o NumberOfPasswordPrompts=1 -o PasswordAuthentication=yes -o KbdInteractiveAuthentication=no -o PreferredAuthentications=password -o PubkeyAuthentication=no",
  1088	            "-av",
  1089	            "src/",
  1090	            "lume@192.168.64.10:/tmp/dest",
  1091	        ]
  1092	    ]
  1093	
  1094	
  1095	def test_transfer_service_scp_uses_fixed_vm_ssh_options(
  1096	    monkeypatch: pytest.MonkeyPatch,
  1097	) -> None:
  1098	    recorded: list[list[str]] = []
  1099	    _, transfer_service, _ = _build_service_stack()
  1100	    monkeypatch.setattr(
  1101	        transfer_service,
  1102	        "_sandbox_command_prefix",
  1103	        lambda: ["sandbox-exec", "-p", "(profile)"],
  1104	    )
  1105	
  1106	    def fake_run(
  1107	        cmd: list[str], check: bool = False
  1108	    ) -> subprocess.CompletedProcess[bytes]:
  1109	        recorded.append(cmd)
  1110	        return subprocess.CompletedProcess(cmd, 0)
  1111	
  1112	    monkeypatch.setattr("talonbox.transfer.subprocess.run", fake_run)
  1113	
  1114	    returncode = transfer_service.scp(["./settings.talon", "guest:/tmp/settings.talon"])
  1115	
  1116	    assert returncode == 0
  1117	    assert recorded == [
  1118	        [
  1119	            "sandbox-exec",
  1120	            "-p",
  1121	            "(profile)",
  1122	            "sshpass",
  1123	            "-p",
  1124	            "lume",
  1125	            "scp",
  1126	            "-o",
  1127	            "StrictHostKeyChecking=no",
  1128	            "-o",
  1129	            "UserKnownHostsFile=/dev/null",
  1130	            "-o",
  1131	            "LogLevel=ERROR",
  1132	            "-o",
  1133	            "BatchMode=no",
  1134	            "-o",
  1135	            "NumberOfPasswordPrompts=1",
  1136	            "-o",
  1137	            "PasswordAuthentication=yes",
  1138	            "-o",
  1139	            "KbdInteractiveAuthentication=no",
  1140	            "-o",
  1141	            "PreferredAuthentications=password",
  1142	            "-o",
  1143	            "PubkeyAuthentication=no",
  1144	            "./settings.talon",
  1145	            "lume@192.168.64.10:/tmp/settings.talon",
  1146	        ]
  1147	    ]
  1148	
  1149	
  1150	def test_transfer_service_sandbox_profile_allows_tmp_and_dev(
  1151	    monkeypatch: pytest.MonkeyPatch,
  1152	) -> None:
  1153	    _, transfer_service, _ = _build_service_stack()
  1154	
  1155	    monkeypatch.setattr(transfer_module, "HOST_OUTPUT_ROOT", Path("/tmp"))
  1156	    monkeypatch.setattr(
  1157	        transfer_service, "_host_output_root", lambda: Path("/private/tmp")
  1158	    )
  1159	
  1160	    profile = transfer_service._sandbox_profile()
  1161	
  1162	    assert "(deny file-write*)" in profile
  1163	    assert '(allow file-write* (subpath "/private/tmp"))' in profile
  1164	    assert '(allow file-write* (subpath "/tmp"))' in profile
  1165	    assert '(allow file-write* (subpath "/dev"))' in profile
  1166	
  1167	
  1168	def test_running_vm_download_uses_scp(monkeypatch: pytest.MonkeyPatch) -> None:
  1169	    recorded: list[list[str]] = []
  1170	    running_vm = _running_vm()
  1171	
  1172	    def fake_run(
  1173	        cmd: list[str],
  1174	        check: bool = False,
  1175	        capture_output: bool = True,
  1176	        text: bool = True,
  1177	        timeout: float | None = None,
  1178	        stdin: object | None = None,
  1179	        input: str | None = None,
  1180	    ) -> subprocess.CompletedProcess[str]:
  1181	        del timeout, stdin, input
  1182	        recorded.append(cmd)
  1183	        return subprocess.CompletedProcess(cmd, 0, "", "")
  1184	
  1185	    monkeypatch.setattr("talonbox.vm.subprocess.run", fake_run)
  1186	
  1187	    running_vm.download("/tmp/out.png", Path("/tmp/out.png"))
  1188	
  1189	    assert recorded == [
  1190	        [
  1191	            "sshpass",
  1192	            "-p",
  1193	            "lume",
  1194	            "scp",
  1195	            "-o",
  1196	            "StrictHostKeyChecking=no",
  1197	            "-o",
  1198	            "UserKnownHostsFile=/dev/null",
  1199	            "-o",
  1200	            "LogLevel=ERROR",
  1201	            "-o",
  1202	            "BatchMode=no",
  1203	            "-o",
  1204	            "NumberOfPasswordPrompts=1",
  1205	            "-o",
  1206	            "PasswordAuthentication=yes",
  1207	            "-o",
  1208	            "KbdInteractiveAuthentication=no",
  1209	            "-o",
  1210	            "PreferredAuthentications=password",
  1211	            "-o",
  1212	            "PubkeyAuthentication=no",
  1213	            "lume@192.168.64.10:/tmp/out.png",
  1214	            "/tmp/out.png",
  1215	        ]
  1216	    ]
  1217	
  1218	
  1219	def test_running_vm_run_repl_retries_transient_ssh_failure(
  1220	    monkeypatch: pytest.MonkeyPatch,
  1221	) -> None:
  1222	    attempts = {"count": 0}
  1223	    running_vm = _running_vm()
  1224	
  1225	    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
  1226	        del kwargs
  1227	        attempts["count"] += 1
  1228	        if attempts["count"] == 1:
  1229	            return subprocess.CompletedProcess(
  1230	                [],
  1231	                255,
  1232	                "",
  1233	                "ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such file or directory\n"
  1234	                "lume@192.168.64.10: Permission denied (publickey,password,keyboard-interactive).",
  1235	            )
  1236	        return subprocess.CompletedProcess([], 0, "ok\n", "")
  1237	
  1238	    monkeypatch.setattr(
  1239	        "talonbox.vm.subprocess.run", lambda *args, **kwargs: fake_run(**kwargs)
  1240	    )
  1241	    monkeypatch.setattr("talonbox.vm.time.sleep", lambda seconds: None)
  1242	
  1243	    result = running_vm.run_repl("print('ok')\n")
  1244	
  1245	    assert result.returncode == 0
  1246	    assert attempts["count"] == 2
  1247	
  1248	
  1249	def test_running_vm_download_retries_transient_ssh_failure(
  1250	    monkeypatch: pytest.MonkeyPatch,
  1251	) -> None:
  1252	    attempts = {"count": 0}
  1253	    running_vm = _running_vm()
  1254	
  1255	    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
  1256	        del kwargs
  1257	        attempts["count"] += 1
  1258	        if attempts["count"] == 1:
  1259	            return subprocess.CompletedProcess(
  1260	                [],
  1261	                255,
  1262	                "",
  1263	                "ssh_askpass: exec(/usr/X11R6/bin/ssh-askpass): No such file or directory\n"
  1264	                "lume@192.168.64.10: Permission denied (publickey,password,keyboard-interactive).",
  1265	            )
  1266	        return subprocess.CompletedProcess([], 0, "", "")
  1267	
  1268	    monkeypatch.setattr(
  1269	        "talonbox.vm.subprocess.run", lambda *args, **kwargs: fake_run(**kwargs)
  1270	    )
  1271	    monkeypatch.setattr("talonbox.vm.time.sleep", lambda seconds: None)
  1272	
  1273	    running_vm.download("/tmp/out.png", Path("/tmp/out.png"))
  1274	
  1275	    assert attempts["count"] == 2
  1276	
  1277	
  1278	def test_running_vm_wait_for_talon_repl_checks_socket_path(
  1279	    monkeypatch: pytest.MonkeyPatch,
  1280	) -> None:
  1281	    running_vm = _running_vm()
  1282	    calls: list[tuple[str | list[str], float, bool, bool]] = []
  1283	
  1284	    def fake_run_shell(
  1285	        command: str | list[str],
  1286	        *,
  1287	        timeout: float | None = None,
  1288	        poll: bool = False,
  1289	        stream: bool = False,
  1290	        check: bool = True,
  1291	    ) -> subprocess.CompletedProcess[str]:
  1292	        del stream
  1293	        calls.append((command, timeout or 0.0, poll, check))
  1294	        return subprocess.CompletedProcess([], 0, "", "")
  1295	
```

## 9. Putting the Pieces Together

Taken as a whole, `talonbox` is a thin CLI on purpose, not because it lacks structure. The code is arranged so each layer has one clear job. `cli.py` defines the human and agent interface. `lume.py` translates the external VM tool into Python objects and failure modes. `vm.py` manages lifecycle and SSH/Talon transport. `transfer.py` enforces the host safety boundary. `talon_client.py` packages the Talon-native operations. `smoke_test.py` composes those capabilities into a realistic diagnostic path, and the tests pin the expected behavior in place.

That layering makes the project easier to extend safely. If the tool grows, new user-facing commands can stay thin and delegate to the existing service objects. If the security model changes, `transfer.py` is the focal point. If startup behavior changes, `VmController.start()` and the smoke-test path will be the canonical places to update. The result is a compact but deliberate codebase: small enough to read linearly, but structured enough that each responsibility has an obvious home.
