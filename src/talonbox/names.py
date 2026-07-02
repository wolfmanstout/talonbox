from __future__ import annotations

import click

TART_NAME_PREFIX = "talonbox-"


def validate_public_vm_name(name: str) -> str:
    if not name:
        raise click.ClickException("VM name must not be empty")
    if "/" in name or ":" in name:
        raise click.ClickException("VM names must not contain '/' or ':'")
    if name.startswith(TART_NAME_PREFIX):
        raise click.ClickException(
            f"Pass the unprefixed VM name; talonbox adds {TART_NAME_PREFIX!r} internally."
        )
    return name


def to_tart_vm_name(public_name: str) -> str:
    return f"{TART_NAME_PREFIX}{validate_public_vm_name(public_name)}"


def to_public_vm_name(tart_name: str) -> str | None:
    if not tart_name.startswith(TART_NAME_PREFIX):
        return None
    public_name = tart_name[len(TART_NAME_PREFIX) :]
    if not public_name:
        return None
    return public_name
