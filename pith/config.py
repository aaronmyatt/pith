"""User-registered commands: global + per-project config for pith.

Two config files, both optional, both the same shape:

  global   $XDG_CONFIG_HOME/pith/config.json   (default: ~/.config/pith/config.json)
  project  <repo root>/.pith.json

Project entries override global ones key-by-key. Shape:

  {
    "commands": {
      "y":      {"run": "scripts/yank.sh", "desc": "yank snippet"},
      "ctrl+t": {"run": "~/bin/ticket.sh", "desc": "file ticket", "suspend": true},
      "Y":      "scripts/quick.sh"
    }
  }

Each command's script is invoked with cwd = repo root, no positional args, and
the selection described entirely in environment variables:

  PITH_ROOT      absolute repo root (== the cwd)
  PITH_FILE      absolute path of the current file
  PITH_REL       repo-relative path of the current file
  PITH_LINE      1-based line of the selection
  PITH_END_LINE  last line of the selection (a def's block end, else PITH_LINE)
  PITH_KIND      def / ref / doc / plain
  PITH_SYMBOL    ancestor chain + the source line (+ first doc line)
  PITH_TARGET    path:line a call/import resolves to (refs only, else empty)

The snippet (PITH_LINE..PITH_END_LINE) is the payload: background commands
read it from stdin; "suspend": true commands keep the terminal as stdin so
they get it in PITH_SNIPPET instead.

"suspend": true leaves the TUI (like editor hand-off) so the script can be
interactive; otherwise it runs in the background and its exit status + last
output line land in the status bar. "timeout" (seconds, background only)
defaults to 60.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Key names Textual accepts: letters/digits plus modifier prefixes like
# "ctrl+t" / "shift+f1". Ref: https://textual.textualize.io/guide/input/#keys
_KEY_RE = re.compile(r"[A-Za-z0-9_+]+")


def global_config_path() -> Path:
    # XDG base-directory spec: config lives under $XDG_CONFIG_HOME, which
    # defaults to ~/.config. Ref: https://specifications.freedesktop.org/basedir-spec/latest/
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pith" / "config.json"


def project_config_path(root: Path) -> Path:
    return root / ".pith.json"


@dataclass
class UserCommand:
    key: str
    run: str            # executable/script; relative paths resolve against repo root
    desc: str
    suspend: bool = False   # True: leave the TUI and run interactively
    timeout: int = 60       # background runs only
    source: str = ""        # "global" | "project" (for status/debug messages)


def _read_commands(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {"__error__": str(path)}
    cmds = data.get("commands", {})
    return cmds if isinstance(cmds, dict) else {}


def load_user_commands(root: Path, reserved: set[str]) -> tuple[dict[str, UserCommand], list[str]]:
    """Merge global + project command configs; returns (commands, warnings)."""
    cmds: dict[str, UserCommand] = {}
    warnings: list[str] = []
    for source, path in (("global", global_config_path()),
                         ("project", project_config_path(root))):
        raw = _read_commands(path)
        if "__error__" in raw:
            warnings.append(f"unreadable config: {raw['__error__']}")
            continue
        for key, spec in raw.items():
            if isinstance(spec, str):            # string shorthand: "y": "script.sh"
                spec = {"run": spec}
            if not isinstance(spec, dict) or not spec.get("run"):
                warnings.append(f"{source} key '{key}': missing \"run\"")
                continue
            if not _KEY_RE.fullmatch(key):
                warnings.append(f"{source} key '{key}': not a valid key name")
                continue
            if key in reserved:
                warnings.append(f"{source} key '{key}': shadows a built-in, skipped")
                continue
            cmds[key] = UserCommand(
                key=key,
                run=str(spec["run"]),
                desc=str(spec.get("desc", spec["run"])),
                suspend=bool(spec.get("suspend", False)),
                timeout=int(spec.get("timeout", 60) or 60),
                source=source,
            )
    return cmds, warnings
