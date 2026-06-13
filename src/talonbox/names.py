from __future__ import annotations

import click

LUME_NAME_PREFIX = "talonbox-"


def validate_public_vm_name(name: str) -> str:
    if not name:
        raise click.ClickException("VM name must not be empty")
    if "/" in name or ":" in name:
        raise click.ClickException("VM names must not contain '/' or ':'")
    if name.startswith(LUME_NAME_PREFIX):
        raise click.ClickException(
            f"Pass the unprefixed VM name; talonbox adds {LUME_NAME_PREFIX!r} internally."
        )
    return name


def to_lume_vm_name(public_name: str) -> str:
    return f"{LUME_NAME_PREFIX}{validate_public_vm_name(public_name)}"


def to_public_vm_name(lume_name: str) -> str | None:
    if not lume_name.startswith(LUME_NAME_PREFIX):
        return None
    public_name = lume_name[len(LUME_NAME_PREFIX) :]
    if not public_name:
        return None
    return public_name
