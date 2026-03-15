# AGENTS.md

## Project Summary

`talonbox` is a local sandbox for testing Talon scripts. Inspired by playwright-cli.

It is an independent, community-driven sandbox for [Talon Voice](https://talonvoice.com/) development. Prefer language that makes that independence clear when describing the project.

## Install (for system-wide use)

Use:

```bash
uv tool install . --reinstall
```

## Repo Norms

- Keep documentation readable for both humans and coding agents.
- When updating the short project description, keep it aligned with the GitHub description: `A local sandbox for testing Talon scripts. Inspired by playwright-cli.`

## Security Principles

- No writes to host files outside `/tmp`.
- No symlink escapes through `/tmp`; do not assume a symlink rooted in `/tmp` makes an out-of-bounds host write acceptable.
- Keep guest and host paths explicit. Use `guest:/...` for guest-side transfer operands.
- Treat the host machine as the thing being protected. Talon execution belongs in the VM unless there is an explicit reason otherwise.
