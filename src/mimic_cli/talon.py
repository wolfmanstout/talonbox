from __future__ import annotations


def build_mimic_payload(command: str) -> str:
    return f"mimic({command!r})\n"


def build_screenshot_payload(remote_path: str) -> str:
    return "\n".join(
        [
            "from talon import screen",
            f"path = {remote_path!r}",
            "img = screen.capture_rect(screen.main().rect, retina=False)",
            "img.save(path) if hasattr(img, 'save') else img.write_file(path)",
            "print(path)",
            "",
        ]
    )
