# talonbox

[![PyPI](https://img.shields.io/pypi/v/talonbox.svg)](https://pypi.org/project/talonbox/)
[![Changelog](https://img.shields.io/github/v/release/wolfmanstout/talonbox?include_prereleases&label=changelog)](https://github.com/wolfmanstout/talonbox/releases)
[![Tests](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml/badge.svg)](https://github.com/wolfmanstout/talonbox/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/wolfmanstout/talonbox/blob/master/LICENSE)

Control Talon in a macOS VM.

## Installation

Install this tool using `pip` or `pipx`:

```bash
pip install talonbox
```

## Usage

`talonbox` is a small set of primitives for coding agents that need to stage Talon
changes inside a local Lume VM before touching the host machine.

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
