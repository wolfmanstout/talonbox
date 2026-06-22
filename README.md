# talonbox

[![PyPI](https://img.shields.io/pypi/v/talonbox.svg)](https://pypi.org/project/talonbox/)
[![Changelog](https://img.shields.io/github/v/release/wolfmanstout/talonbox?include_prereleases&label=changelog)](https://github.com/wolfmanstout/talonbox/releases)
[![Tests](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml/badge.svg)](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/wolfmanstout/talonbox/blob/main/LICENSE)

`talonbox` is a community-built sandbox that lets coding agents test
[Talon Voice](https://talonvoice.com/) scripts in disposable macOS VMs before
touching the host machine.

## Installation

Install [Lume](https://github.com/trycua/cua/tree/main/libs/lume) first. See
the [Lume installation docs](https://docs.trycua.com/docs/lume/installation),
or use Homebrew:

```bash
brew install lume
```

`talonbox` uses Lume to create, clone, start, stop, and inspect macOS VMs. You
can also use `lume` directly when you need its lower-level VM management CLI.
[OpenClaw recommends Lume](https://docs.openclaw.ai/install/macos-vm#macos-vm-options)
for sandboxed macOS VMs on Apple Silicon Macs.

Install with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install talonbox
```

You can also install it with `pip` or `pipx`:

```bash
pip install talonbox
pipx install talonbox
```

## Initial Setup

`talonbox` is designed to be a CLI that you point your coding agent at when it
needs to test Talon changes. The agent can clone a disposable VM, sync scripts
into it, run Talon commands with `mimic`, capture screenshots, and bring
artifacts back under `/tmp` without writing arbitrary files on the host.

Start by asking your agent to help create a stopped, Talon-ready source VM that
future agents can clone for experiments. For the default public Talon build,
use this prompt:

```text
Help me create the talonbox golden VM.
Run `talonbox create --base tahoe-base golden` and follow the printed setup
instructions.
```

If you test Talon beta builds, add one more sentence to the prompt, adjusting
the path to the downloaded beta DMG on your machine:

```text
Add `--talon-dmg ~/Downloads/talon-beta.dmg` to the `talonbox create` command.
```

`talonbox create` prints setup instructions for a human or agent to follow.
The `--base` option names a reusable base OS VM before Talon is set up.
Expect the first golden VM setup to be time-consuming and somewhat
error-prone: macOS setup screens, Lume automation, Talon first-run prompts, and
privacy permissions all need to line up. That setup friction is worth getting
through, and it is not representative of the normal talonbox experience. Once a
golden VM passes `smoke-test`, talonbox should feel fast and magical.

When an agent creates a VM, it will try by default to do as much of the setup
as it safely can, stopping for human-only steps such as accepting the Talon
EULA. The first setup can take over an hour. To reduce repeated permission
interruptions, consider allowlisting talonbox commands in your agent client,
including `talonbox ...` and, when working from this checkout,
`uv run talonbox ...`.

If you would rather save wall-clock time and agent tokens, add this to the
prompt. The tradeoff is that the agent will pause more often for manual VNC
handoffs instead of working through GUI prompts itself:

```text
During VM creation, optimize for saving wall-clock time and agent tokens. When
you reach a macOS or Talon GUI prompt, give me the VNC URL and
`talonbox open NAME`, then wait for me instead of navigating it yourself.
```

The Lume VM user is `lume`, and the default Lume password is `lume`. The VM
should auto-login, but you may occasionally need these for permissions dialogs.

## Usage

For top-level help, run:

```bash
talonbox --help
```

You can also run `talonbox` commands manually if you'd like:

```bash
talonbox clone golden experiment
talonbox start experiment
talonbox rsync -a ~/.talon/user/ experiment:/Users/lume/.talon/user/
talonbox mimic experiment "focus chrome"
talonbox click experiment 400 300
talonbox type experiment "hello from Talon"
talonbox screenshot experiment /tmp/talon.png
talonbox open experiment
talonbox stop experiment
```

Clones use APFS copy-on-write, so the actual disk usage is much more efficient
than the apparent full VM size.

For a first-pass diagnostic when the setup seems broken, run:

```bash
talonbox smoke-test golden
```

`smoke-test` checks a stopped source VM through a temporary clone.

## Agent Instructions

`talonbox` works with different cloning, stopping, and deletion workflows. Add
the policy you prefer to your project's `AGENTS.md` file or to a reusable agent
skill. macOS Virtualization commonly allows only two active VMs, so keep source
VMs stopped and stop test VMs when each test is complete.

Drop-in guidance for a simple single-test-VM workflow:

```markdown
Use `talonbox` for Talon tests. Read `talonbox --help` before choosing
commands. Keep `golden` stopped and clean. Use one working VM named `test` for
experiments. Before testing, run `talonbox list` or `talonbox status test`; if
`test` does not exist, clone it from `golden`. Sync the current repo into the
VM, run the relevant `mimic` commands, capture screenshots or logs under
`/tmp`, then stop `test` when done. Ask before deleting `test` unless the user
explicitly requested a clean VM.
```

Drop-in guidance for isolated multi-test workflows:

```markdown
Use `talonbox` with disposable, task-specific clones. Read `talonbox --help`
before choosing commands. Prefer `talonbox clone golden <task-name>` before
each test or experiment, using a readable name such as
`test-cursorless-snippets` or `debug-dictation-timeout`. Start that VM, sync
the repo into it, run the relevant `mimic` commands, capture screenshots or
logs under `/tmp`, then stop the VM when done. Keep `golden` stopped and clean.
Stop completed test VMs before starting more. Do not delete the task VM until
the user has approved the result; after approval and commit, delete it.
```

## Security Principles

`talonbox` is a best-effort safety layer for keeping Talon experimentation
contained and predictable, especially when it is driven by coding agents. Bugs
may exist, and the project maintainers are not responsible for damage, data
loss, or unexpected host or VM changes.

The guiding principles are:

- No caller-triggered writes to host files outside `/tmp`. A `talonbox` command should not let its caller cause arbitrary host writes beyond that boundary.
- Prefer explicit guest/host boundaries. Remote paths must be written as `NAME:/...` so transfers stay easy to audit.
- Favor VM-local execution first. Talon code should run in the guest and only copy explicit outputs back to the host.

Talonbox VMs do have network access, so data exfiltration due to malicious 
prompt injection is possible.

## Development

To contribute to this tool, use [uv](https://docs.astral.sh/uv/). The following
command will establish the venv and run tests:

```bash
uv run pytest
```

To run talonbox locally, use:

```bash
uv run talonbox
```
