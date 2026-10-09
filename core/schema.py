"""The JSON script format, shared by the engine and the form-based builder.

A script file looks like::

    {
      "name": "Copy task between apps",
      "steps": [
        {"action": "open_app", "package": "com.example.app"},
        {"action": "wait_for_element", "locator_type": "id",
         "locator_value": "com.example.app:id/title", "timeout_seconds": 15},
        {"action": "click", "locator_type": "id", "locator_value": "com.example.app:id/button"},
        {"action": "copy_text", "locator_type": "id",
         "locator_value": "com.example.app:id/text_field", "save_as": "copied_value"},
        {"action": "scroll", "direction": "down", "times": 2},
        {"action": "paste_text", "locator_type": "id",
         "locator_value": "com.other.app:id/input_field", "value_from": "copied_value"},
        {"action": "wait", "seconds": 3}
      ]
    }

``ACTIONS`` below is the single source of truth for which fields each action
takes; the builder generates its forms from it and the engine validates
against it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOCATOR_TYPES = ["id", "xpath", "accessibility id", "text", "class name", "android uiautomator"]
DIRECTIONS = ["up", "down", "left", "right"]
DEFAULT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class FieldSpec:
    """One input of an action, as shown in the builder and checked by the validator."""

    name: str
    label: str
    kind: str  # "text", "int", "float", "choice", "locator_type"
    required: bool = True
    default: Any = None
    choices: tuple[str, ...] = ()
    help: str = ""
    minimum: float | None = None


@dataclass(frozen=True)
class ActionSpec:
    key: str
    label: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)


_LOCATOR_FIELDS = (
    FieldSpec("locator_type", "Locator type", "locator_type", default="id", choices=tuple(LOCATOR_TYPES)),
    FieldSpec("locator_value", "Locator value", "text",
              help="e.g. com.example.app:id/button, or an XPath, or an accessibility label"),
)
_TIMEOUT_FIELD = FieldSpec("timeout_seconds", "Timeout (seconds)", "float", required=False,
                           default=DEFAULT_TIMEOUT_SECONDS, minimum=0)

ACTIONS: dict[str, ActionSpec] = {
    spec.key: spec
    for spec in (
        ActionSpec("open_app", "Open App", (
            FieldSpec("package", "App package ID", "text", help="e.g. com.whatsapp"),
        )),
        ActionSpec("click", "Click", _LOCATOR_FIELDS + (_TIMEOUT_FIELD,)),
        ActionSpec("scroll", "Scroll", (
            FieldSpec("direction", "Direction", "choice", default="down", choices=tuple(DIRECTIONS)),
            FieldSpec("times", "Times", "int", default=1, minimum=1),
        )),
        ActionSpec("wait", "Wait", (
            FieldSpec("seconds", "Seconds", "float", default=1, minimum=0),
        )),
        ActionSpec("wait_for_element", "Wait For Element", _LOCATOR_FIELDS + (
            FieldSpec("timeout_seconds", "Timeout (seconds)", "float",
                      default=DEFAULT_TIMEOUT_SECONDS, minimum=0),
        )),
        ActionSpec("copy_text", "Copy Text", _LOCATOR_FIELDS + (
            FieldSpec("save_as", "Save as (variable name)", "text", default="copied_value",
                      help="Name used later by a Paste Text step"),
            _TIMEOUT_FIELD,
        )),
        ActionSpec("paste_text", "Paste Text", _LOCATOR_FIELDS + (
            FieldSpec("value_from", "Paste variable", "text", required=False, default="copied_value",
                      help="Variable saved by an earlier Copy Text step"),
            FieldSpec("text", "...or fixed text", "text", required=False,
                      help="Used when no variable is given"),
            _TIMEOUT_FIELD,
        )),
    )
}

ACTION_LABELS = {key: spec.label for key, spec in ACTIONS.items()}

_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ScriptError(ValueError):
    """Raised when a script file cannot be loaded or is malformed."""


def validate_step(step: dict) -> dict[str, str]:
    """Return ``{field_name: error_message}``; empty when the step is valid.

    The special key ``"action"`` reports a problem with the action itself.
    """
    action = step.get("action")
    if action not in ACTIONS:
        return {"action": f"Unknown action {action!r}"}

    errors: dict[str, str] = {}
    for spec in ACTIONS[action].fields:
        value = step.get(spec.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if spec.required:
                errors[spec.name] = f"{spec.label} is required"
            continue
        if spec.kind in ("int", "float"):
            try:
                number = int(value) if spec.kind == "int" else float(value)
            except (TypeError, ValueError):
                errors[spec.name] = f"{spec.label} must be a number"
                continue
            if spec.minimum is not None and number < spec.minimum:
                errors[spec.name] = f"{spec.label} must be at least {spec.minimum:g}"
        elif spec.kind in ("choice", "locator_type") and value not in spec.choices:
            errors[spec.name] = f"{spec.label} must be one of: {', '.join(spec.choices)}"
        elif spec.name in ("save_as", "value_from") and not _VARIABLE_NAME.match(str(value)):
            errors[spec.name] = "Use letters, digits and underscores only"

    if action == "paste_text" and not step.get("value_from") and not step.get("text"):
        errors["value_from"] = "Choose a variable to paste, or enter fixed text"
    return errors


def validate_script(script: dict) -> list[str]:
    """Return human-readable problems with a whole script; empty when valid."""
    problems: list[str] = []
    if not isinstance(script, dict):
        return ["Script must be a JSON object"]
    if not str(script.get("name", "")).strip():
        problems.append("Script needs a name")
    steps = script.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("Script needs at least one step")
        return problems
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            problems.append(f"Step {index}: must be an object")
            continue
        for message in validate_step(step).values():
            problems.append(f"Step {index} ({step.get('action')}): {message}")
    return problems


def normalize_step(step: dict) -> dict:
    """Drop empty optional fields and coerce numbers, so saved JSON stays tidy."""
    action = step["action"]
    clean: dict[str, Any] = {"action": action}
    for spec in ACTIONS[action].fields:
        value = step.get(spec.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if spec.kind == "int":
            value = int(value)
        elif spec.kind == "float":
            number = float(value)
            value = int(number) if number.is_integer() else number
        elif isinstance(value, str):
            value = value.strip()
        clean[spec.name] = value
    return clean


def describe_step(step: dict) -> str:
    """One-line human summary of a step, used on builder cards and in logs."""
    action = step.get("action")
    label = ACTION_LABELS.get(action, str(action))
    if action == "open_app":
        return f"{label}: {step.get('package')}"
    if action == "scroll":
        return f"{label} {step.get('direction', 'down')} × {step.get('times', 1)}"
    if action == "wait":
        return f"{label} {step.get('seconds')}s"
    target = f"{step.get('locator_type')}={step.get('locator_value')}"
    if action == "copy_text":
        return f"{label} from {target} → {step.get('save_as')}"
    if action == "paste_text":
        source = f"${step['value_from']}" if step.get("value_from") else repr(step.get("text", ""))
        return f"{label} {source} into {target}"
    if action == "wait_for_element":
        return f"{label} {target} (up to {step.get('timeout_seconds', DEFAULT_TIMEOUT_SECONDS)}s)"
    return f"{label} {target}"


def load_script(path: str | Path) -> dict:
    """Read and validate a JSON script file."""
    path = Path(path)
    try:
        script = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScriptError(f"Could not read {path.name}: {exc}") from exc
    problems = validate_script(script)
    if problems:
        raise ScriptError(f"{path.name} is invalid:\n  " + "\n  ".join(problems))
    return script


def safe_filename(name: str) -> str:
    """Turn a script name into a filesystem-safe file stem."""
    stem = re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip().replace(" ", "_")
    return stem or "script"


def save_script(script: dict, folder: str | Path) -> Path:
    """Validate, normalize and write a script to ``folder/<name>.json``."""
    problems = validate_script(script)
    if problems:
        raise ScriptError("\n".join(problems))
    data = {"name": script["name"].strip(), "steps": [normalize_step(s) for s in script["steps"]]}
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_filename(data['name'])}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
