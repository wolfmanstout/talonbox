# talonbox

[![PyPI](https://img.shields.io/pypi/v/talonbox.svg)](https://pypi.org/project/talonbox/)
[![Changelog](https://img.shields.io/github/v/release/wolfmanstout/talonbox?include_prereleases&label=changelog)](https://github.com/wolfmanstout/talonbox/releases)
[![Tests](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml/badge.svg)](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/wolfmanstout/talonbox/blob/main/LICENSE)

`talonbox` is a community-built sandbox that lets coding agents test
[Talon Voice](https://talonvoice.com/) scripts in disposable macOS VMs before
touching the host machine.

## Installation

Install with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install talonbox
```

You can also install it with `pip` or `pipx`:

```bash
pip install talonbox
pipx install talonbox
```

## Usage

`talonbox` is designed to be a CLI that you point your coding agent at when it
needs to test Talon changes. The agent can clone a disposable VM, sync scripts
into it, run Talon commands with `mimic`, capture screenshots, and bring
artifacts back under `/tmp` without writing arbitrary files on the host.

Start by asking your agent to help create a stopped, Talon-ready source VM that
future agents can clone for experiments. For the default public Talon build,
use this prompt:

```text
Help me create the default talonbox golden VM for public Talon.
Run talonbox create golden, follow the printed setup instructions, and pause
when I need to handle GUI prompts, permissions, or the Talon EULA.
```

If you test Talon beta builds, add one more sentence to the prompt:

```text
Use ~/Downloads/talon-beta.dmg as the Talon DMG, adjusting the path to the
downloaded beta DMG on this machine.
```

`create` prints setup instructions for a human or agent to follow. Setup
requires human decisions, GUI permission prompts, and Talon EULA acceptance.
Expect the first golden VM setup to be time-consuming and somewhat
error-prone: macOS setup screens, Lume automation, Talon first-run prompts, and
privacy permissions all need to line up. That setup friction is worth getting
through, and it is not representative of the normal talonbox experience. Once a
golden VM passes `smoke-test`, cloning it for future Talon experiments should
feel fast, disposable, and a little bit magical.

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
- No symlink escapes through `/tmp`; a symlink placed under `/tmp` should not be able to redirect writes outside the allowed boundary.
- On macOS, treat `/private/tmp` as the canonical form of the same allowed temp root, not as a separate exception.
- Prefer explicit guest/host boundaries. Remote paths must be written as `NAME:/...` so transfers stay easy to audit.
- Favor VM-local execution first. Talon code should run in the guest and only copy explicit outputs back to the host.

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
