# talonbox

[![PyPI](https://img.shields.io/pypi/v/talonbox.svg)](https://pypi.org/project/talonbox/)
[![Changelog](https://img.shields.io/github/v/release/wolfmanstout/talonbox?include_prereleases&label=changelog)](https://github.com/wolfmanstout/talonbox/releases)
[![Tests](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml/badge.svg)](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/wolfmanstout/talonbox/blob/master/LICENSE)

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
talonbox start
talonbox rsync -av ~/.talon/user/ guest:/Users/lume/.talon/user/
talonbox mimic "focus chrome"
talonbox screenshot /tmp/talon.png
talonbox stop
```

General guest access:

```bash
talonbox exec -- whoami
talonbox scp guest:/tmp/out.png /tmp/out.png
printf 'print(1 + 1)\n' | talonbox repl
```

You can also run:

```bash
python -m talonbox --help
```

## Security Principles

These principles are meant to keep Talon experimentation contained and predictable:

- No writes to host files outside `/tmp`.
- No symlink escapes through `/tmp`; a symlink placed under `/tmp` should not be able to redirect writes outside the allowed boundary.
- Prefer explicit guest/host boundaries. Remote paths must be written as `guest:/...` so transfers stay easy to audit.
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
