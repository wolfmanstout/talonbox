# AGENTS.md

## Project Summary

`talonbox` is a local sandbox for testing Talon scripts. Inspired by playwright-cli.

It is an independent, community-driven sandbox for [Talon Voice](https://talonvoice.com/) development. Prefer language that makes that independence clear when describing the project.

## Install (for system-wide use)

Use:

```bash
uv tool install . --reinstall
```

After committing changes meant for the installed CLI, run the same command again to refresh the system-wide `talonbox` tool from the current checkout.

## Run During Development

When working from this repository, run `talonbox` via `uv run` so commands use the code in the checkout:

```bash
uv run talonbox ...
```

Use the installed `talonbox` binary only when you specifically want to verify the system-wide install behavior.

## Repo Norms

- Keep documentation readable for both humans and coding agents.
- When updating the short project description, keep it aligned with the GitHub description: `A local sandbox for testing Talon scripts. Inspired by playwright-cli.`

## Security Principles

- No caller-triggered writes to host files outside `/tmp`. `talonbox` should not let humans or coding agents cause arbitrary host writes beyond that boundary.
- No symlink escapes through `/tmp`; do not assume a symlink rooted in `/tmp` makes an out-of-bounds host write acceptable.
- On macOS, treat `/private/tmp` as the canonical form of the same allowed temp root, not as a separate exception.
- Keep guest and host paths explicit. Use `guest:/...` for guest-side transfer operands.
- Treat the host machine as the thing being protected. Talon execution belongs in the VM unless there is an explicit reason otherwise.
