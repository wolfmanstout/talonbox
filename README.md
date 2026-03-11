# mimic-cli

[![PyPI](https://img.shields.io/pypi/v/mimic-cli.svg)](https://pypi.org/project/mimic-cli/)
[![Changelog](https://img.shields.io/github/v/release/wolfmanstout/mimic-cli?include_prereleases&label=changelog)](https://github.com/wolfmanstout/mimic-cli/releases)
[![Tests](https://github.com/wolfmanstout/mimic-cli/actions/workflows/test.yml/badge.svg)](https://github.com/wolfmanstout/mimic-cli/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/wolfmanstout/mimic-cli/blob/master/LICENSE)

Control Talon in a macOS VM.

## Installation

Install this tool using `pip` or `pipx`:

```bash
pip install mimic-cli
```

## Usage

`mimic-cli` is a small set of primitives for coding agents that need to stage Talon
changes inside a local Lume VM before touching the host machine.

For top-level help, run:

```bash
mimic-cli --help
```

Typical workflow:

```bash
mimic-cli start
mimic-cli rsync -av ~/.talon/user/ /Users/lume/.talon/user/
mimic-cli mimic "focus chrome"
mimic-cli screenshot /tmp/talon.png
mimic-cli stop
```

General guest access:

```bash
mimic-cli exec -- whoami
printf 'print(1 + 1)\n' | mimic-cli repl
```

You can also run:

```bash
python -m mimic_cli --help
```

## Development

To contribute to this tool, use uv. The following command will establish the
venv and run tests:

```bash
uv run pytest
```

To run mimic-cli locally, use:

```bash
uv run mimic-cli
```
