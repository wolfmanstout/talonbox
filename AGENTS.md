# AGENTS.md

## Project Summary

`talonbox` is a community-built local sandbox that lets coding agents test
[Talon Voice](https://talonvoice.com/) scripts in disposable macOS VMs before
touching the host machine.

## Install (for system-wide use)

Use:

```bash
uv tool install . --reinstall
```

After pushing changes to the main branch, run the same command again to refresh the system-wide `talonbox` tool from the current checkout.

## Run During Development

When working from this repository, run `talonbox` via `uv run` so commands use the code in the checkout:

```bash
uv run talonbox ...
```

Use the installed `talonbox` binary only when you specifically want to verify the system-wide install behavior.

## Repo Norms

- Keep documentation readable for both humans and coding agents.

## Security Principles

- No caller-triggered writes to host files outside `/tmp`. `talonbox` should not let humans or coding agents cause arbitrary host writes beyond that boundary.
- No symlink escapes through `/tmp`; do not assume a symlink rooted in `/tmp` makes an out-of-bounds host write acceptable.
- On macOS, treat `/private/tmp` as the canonical form of the same allowed temp root, not as a separate exception.
- Treat the host machine as the thing being protected. Talon execution belongs in the VM unless there is an explicit reason otherwise.
