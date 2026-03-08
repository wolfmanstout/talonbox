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

For help, run:

```bash
mimic-cli --help
```

You can also use:

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
