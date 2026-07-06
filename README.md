# Talonbox

[![PyPI](https://img.shields.io/pypi/v/talonbox.svg)](https://pypi.org/project/talonbox/)
[![Changelog](https://img.shields.io/github/v/release/wolfmanstout/talonbox?include_prereleases&label=changelog)](https://github.com/wolfmanstout/talonbox/releases)
[![Tests](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml/badge.svg)](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/wolfmanstout/talonbox/blob/main/LICENSE)

Talonbox is a local sandbox that lets coding agents test [Talon
Voice](https://talonvoice.com/) scripts in disposable macOS VMs.

## Installation

Install [Tart](https://tart.run/) first. See the
[Tart quick start](https://tart.run/quick-start/), or use Homebrew:

```bash
brew install cirruslabs/cli/tart
brew install cirruslabs/cli/sshpass
```

Talonbox uses Tart to manage macOS VMs. You can also use `tart` directly when
you need its lower-level VM management CLI.

Install Talonbox with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install talonbox
```

You can also install it with `pip` or `pipx`:

```bash
pip install talonbox
pipx install talonbox
```

## Initial Setup

Talonbox is designed to be a CLI that you point your coding agent to when it
needs to test Talon changes. The agent can clone a disposable VM, sync scripts
into it, run Talon commands with `mimic`, capture screenshots, and bring
files back to the host.

Start by asking your agent to help create a VM that can be cloned for
experiments. For the default public Talon build, use this prompt:

```text
Help me create the Talonbox golden VM.
Run `talonbox create --base tahoe-base golden` and follow the printed setup
instructions.
```

Note that "tahoe-base" and "golden" are arbitrary VM names; use whatever you
prefer.

If you use beta Talon, add one more sentence to the prompt, adjusting the path
to the downloaded beta DMG on your machine or its URL:

```text
Add `--talon-dmg ~/Downloads/talon-beta.dmg` to the `talonbox create` command.
```

`talonbox create` prints setup instructions for a human or agent to follow. The
`--base` option names a reusable base OS VM before Talon is set up. Expect the
first golden VM setup to be time-consuming and somewhat error-prone: Talon
first-run prompts, configuration, and privacy permissions all need to line up.
That setup friction is worth getting through, and it is not representative of
the normal Talonbox experience. Once a golden VM passes `smoke-test`, Talonbox
should feel fast and magical.

When an agent creates a VM, it will try by default to do as much of the setup
as it safely can, stopping for human-only steps such as accepting the Talon
EULA. The first setup can take over an hour. To reduce repeated permission
interruptions, consider allowlisting Talonbox commands in your agent client.

If you would rather save wall-clock time and agent tokens, add this to the
prompt. The tradeoff is that the agent will pause more often for manual VNC
handoffs instead of working through GUI prompts itself:

```text
During VM creation, optimize for saving wall-clock time and agent tokens. When
you reach a macOS or Talon GUI prompt, give me the VNC URL and
`talonbox open NAME`, then wait for me instead of navigating it yourself.
```

The Tart VM user is `admin`, and the default password is `admin`. The VM should
auto-login, but you may occasionally need these for permissions dialogs.

## Usage

Just point your coding agent to `talonbox --help` and tell it what to test.

Here is what a typical sequence of Talonbox commands might look like:

```bash
talonbox clone golden experiment
talonbox start experiment
talonbox rsync -a ~/.talon/user/ experiment:/Users/admin/.talon/user/
talonbox mimic experiment "focus chrome"
talonbox click experiment 400 300
talonbox type experiment "hello from Talon"
talonbox screenshot experiment /tmp/talon.png
talonbox stop experiment
talonbox delete experiment
```

Clones use [APFS
copy-on-write](https://en.wikipedia.org/wiki/Apple_File_System#Clones), so the
actual disk usage is *much* more efficient than the apparent file size (only
file _changes_ take space).

By default, `talonbox stop` suspends the running VM so it can be restored
exactly as-is, which uses a few GB for the snapshot. If you would prefer to shut
it down instead, use `talonbox stop --shutdown`.

For a first-pass diagnostic when the setup seems broken, run:

```bash
talonbox smoke-test golden  # or whatever VM you clone from
```

`smoke-test` checks a source VM through a temporary clone. Tart clones should
come from a fully stopped VM, so run `talonbox stop --shutdown golden` before
cloning or smoke testing if the source is suspended or running.

## Agent Instructions

Talonbox works with different cloning, stopping, and deletion workflows. Add the
policy you prefer to your project's `AGENTS.md` file or to a reusable agent
skill. macOS Virtualization commonly allows only two active VMs, so keep source
VMs inactive and stop test VMs when each test is complete.

Drop-in guidance for a simple single-test-VM workflow:

```markdown
Use Talonbox to test Talon scripts end-to-end.
Read `talonbox --help` before choosing commands.

Keep `golden` inactive and clean. Use one working VM named `test` for
experiments.

Before testing, run `talonbox list` or `talonbox status test`. If `test` does
not exist, make sure `golden` is fully stopped with
`talonbox stop --shutdown golden`, then clone it.

Sync the current repo into the VM, run the relevant `mimic` commands, capture
screenshots or logs under `/tmp`, then stop `test` when done.
```

Drop-in guidance for isolated multi-test workflows (more complex, but
recommended):

```markdown
Use Talonbox with disposable, task-specific clones to test Talon scripts
end-to-end.
Read `talonbox --help` before choosing commands.

Prefer `talonbox clone golden <task-name>` before each test or experiment, using
a readable name such as `test-cursorless-snippets` or `debug-dictation-timeout`.

Start that VM, sync the repo into it, run the relevant `mimic` commands, capture
screenshots or logs under `/tmp`, then stop the VM when done.

Keep `golden` inactive and clean. Run `talonbox stop --shutdown golden` before
cloning it if needed. Stop completed test VMs before starting more.

Do not shut down with `--shutdown` or delete the task VM until the user has
approved the result. After approval and commit, delete it.
```

## Security Principles

Talonbox is a best-effort safety layer for keeping agent-driven Talon
experimentation contained and predictable. It is designed to work alongside the
default agent sandboxes provided by Codex and Claude Code. Bugs may exist, and
the project maintainers are not responsible for damage, data loss, or unexpected
host or VM changes.

The guiding principles are:

- No caller-triggered writes to host files outside `/tmp`: a `talonbox` command
  should not let its caller cause arbitrary host writes beyond that boundary.
- Prefer explicit guest/host boundaries. Remote paths must be written as
  `NAME:/...` so transfers stay easy to audit.
- Favor VM-local execution first. Talon code should run in the guest and only
  copy explicit outputs back to the host.

Talonbox VMs do have network access, so data exfiltration due to malicious
prompt injection is possible. Use caution when mixing Talonbox with untrusted
inputs in an agent thread.

## Development

To contribute to this tool, use [uv](https://docs.astral.sh/uv/). The following
command will establish the venv and run tests:

```bash
uv run pytest
```

To run Talonbox locally, use:

```bash
uv run talonbox
```
