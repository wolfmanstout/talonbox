# talonbox

[![PyPI](https://img.shields.io/pypi/v/talonbox.svg)](https://pypi.org/project/talonbox/)
[![Changelog](https://img.shields.io/github/v/release/wolfmanstout/talonbox?include_prereleases&label=changelog)](https://github.com/wolfmanstout/talonbox/releases)
[![Tests](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml/badge.svg)](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/wolfmanstout/talonbox/blob/main/LICENSE)

A local sandbox for testing Talon scripts. Inspired by playwright-cli.

`talonbox` is an independent, community-driven sandbox for [Talon Voice](https://talonvoice.com/) development. It gives humans and coding agents a safer place to stage Talon changes inside a local Lume VM before touching the host machine.

## Installation

Install with `uv`:

```bash
uv tool install talonbox
```

You can also install it with `pip` or `pipx`:

```bash
pip install talonbox
pipx install talonbox
```

## Usage

`talonbox` provides a small set of primitives for testing Talon scripts in a VM-backed sandbox.

For top-level help, run:

```bash
talonbox --help
```

Typical workflow:

```bash
talonbox clone golden experiment
talonbox start experiment
talonbox rsync -av ~/.talon/user/ experiment:/Users/lume/.talon/user/
talonbox mimic experiment "focus chrome"
talonbox screenshot experiment /tmp/talon.png
talonbox stop experiment
```

Commands take the VM name as a positional argument. Use `list` to see available
VMs, `status` for connection details, and `start` to resume an existing VM.
Transfers use `NAME:/absolute/path` for the VM side, which keeps host and guest
paths easy to distinguish.

Cloning is explicit:

```bash
talonbox clone golden experiment
```

`clone` delegates to `lume clone`, which uses APFS copy-on-write cloning on
macOS for low-overhead VM copies. `start` never clones, deletes, or wipes a VM;
it starts or resumes an existing VM and starts Talon if Talon is not already
running.

For a first-pass diagnostic when the setup seems broken, run:

```bash
talonbox smoke-test golden
```

`smoke-test` is a mutating end-to-end sanity check against a temporary clone.
The source VM must be stopped. It clones the source, pushes a temporary Talon
command bundle into the clone, runs `mimic`, captures screenshots, keeps
debugging artifacts under `/tmp`, then stops and deletes the clone.

General guest access:

```bash
talonbox exec experiment -- whoami
talonbox scp experiment:/tmp/out.png /tmp/out.png
printf 'print(1 + 1)\n' | talonbox repl experiment
```

Host-side outputs from `rsync`, `scp`, and `screenshot` are intentionally restricted to `/tmp`.
This is a caller-facing safety guarantee: invoking `talonbox` must not let humans or coding agents cause arbitrary host writes outside `/tmp`.
On macOS, `/tmp` may resolve to `/private/tmp`; that canonical temp root is still allowed, but symlink escapes rooted under it are rejected.
For `rsync` and `scp`, talonbox requires explicit `NAME:/...` VM transfer operands and runs the underlying transfer process inside the macOS sandbox, so extra host-side writes outside that boundary fail with an obvious permission error instead of relying on a large flag denylist.

You can also run:

```bash
python -m talonbox --help
```

## Security Principles

These principles are meant to keep Talon experimentation contained and predictable, especially when `talonbox` is driven by coding agents:

- No caller-triggered writes to host files outside `/tmp`. A `talonbox` command should not let its caller cause arbitrary host writes beyond that boundary.
- No symlink escapes through `/tmp`; a symlink placed under `/tmp` should not be able to redirect writes outside the allowed boundary.
- On macOS, treat `/private/tmp` as the canonical form of the same allowed temp root, not as a separate exception.
- Prefer explicit guest/host boundaries. Remote paths must be written as `NAME:/...` so transfers stay easy to audit.
- Favor VM-local execution first. Talon code should run in the guest and only copy explicit outputs back to the host.

## Development

To contribute to this tool, use uv. The following command will establish the
venv and run tests:

```bash
uv run pytest
```

To run talonbox locally, use:

```bash
uv run talonbox
```
