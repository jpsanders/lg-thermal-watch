"""The packaged manual: ``lg-thermal-watch guide`` prints it and ``lg-thermal-watch man`` shows it as a man page."""

import os
import shutil
import subprocess  # nosec B404: runs man with fixed arguments only
import sys
from importlib import resources
from pathlib import Path

PROG = "lg-thermal-watch"


def manual() -> str:
    """
    Return the full manual as Markdown.

    :returns: The manual
    """
    return (resources.files("lg_thermal_watch") / "data" / "MANUAL.md").read_text(encoding="utf-8")


def show_man(install: bool, raw: bool) -> None:
    """
    Show the man page, print its roff source, or install it so ``man lg-thermal-watch`` works.

    :param install: Write it to the per user man folder
    :param raw: Print the roff source
    """
    page = resources.files("lg_thermal_watch") / "data" / f"{PROG}.1"
    if install:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        target = Path(base) / "man" / "man1" / f"{PROG}.1"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"installed {target}; open it with: man {PROG}")
        return
    man = shutil.which("man")
    if man and sys.stdout.isatty() and not raw:
        with resources.as_file(page) as path:
            subprocess.call([man, "-l", str(path)])  # nosec B603: man, fixed arguments
        return
    print(page.read_text(encoding="utf-8"), end="")
