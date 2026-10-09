"""The JSON script format, shared by the engine and the form-based builder.

A script file looks like::

    {
      "name": "Copy order to the other phone",
      "steps": [
        {"action": "open_app", "package": "com.shop.app", "device": "A"},
        {"action": "copy_text", "locator_type": "id", "locator_value": "com.shop.app:id/order",
         "save_as": "order", "device": "A", "retries": 2},
        {"action": "if_exists", "locator_type": "text", "locator_value": "Paid", "device": "A",
         "then": [{"action": "set_variable", "name": "status", "value": "paid"}],
         "else": [{"action": "set_variable", "name": "status", "value": "unpaid"}]},
        {"action": "repeat", "times": 3, "steps": [
          {"action": "scroll", "direction": "down", "device": "B"}
        ]},
        {"action": "paste_text", "locator_type": "id", "locator_value": "com.notes:id/body",
         "text": "Order {{order}} is {{status}}", "device": "B", "on_fail": "stop"}
      ]
    }

* ``device`` names a *role* ("A", "B", "Phone 1"...). When a script uses two or
  more roles it is a cross-phone workflow: at run time each role is mapped to
  a real phone and steps run in order across them, sharing variables. Steps
  without ``device`` run on the first role. A script with no roles runs
  independently on each phone it is started on.
* ``repeat`` and ``if_exists`` are blocks holding nested steps.
* Every device step accepts ``retries`` (extra attempts) and ``on_fail``
  (``"skip"`` — log and carry on, the default — or ``"stop"``).
* Text fields accept ``{{variable}}`` placeholders; built-ins are
  ``{{loop_index}}``, ``{{run_number}}``, ``{{device}}``, ``{{date}}`` and ``{{time}}``.

``ACTIONS`` below is the single source of truth for which fields each action
takes; the builder generates its forms from it and the engine validates
against it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

LOCATOR_TYPES = ["id", "xpath", "accessibility id", "text", "class name", "android uiautomator"]
DIRECTIONS = ["up", "down", "left", "right"]
KEYS = ["back", "home", "enter", "recent_apps", "delete", "search", "menu", "volume_up", "volume_down"]
ON_FAIL = ["skip", "stop"]
DEFAULT_TIMEOUT_SECONDS = 15
MAX_NESTING = 5


@dataclass(frozen=True)
class FieldSpec:
    """One input of an action, as shown in the builder and checked by the validator."""

    name: str
    label: str
    kind: str  # "text", "int", "float", "choice", "locator_type", "bool"
    required: bool = True
    default: Any = None
    choices: tuple[str, ...] = ()
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    templated: bool = False  # accepts {{variable}} placeholders


@dataclass(frozen=True)
class ActionSpec:
    key: str
    label: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)
    group: str = "Basic"
    uses_device: bool = True          # runs on a phone (so takes device/retries/on_fail)
    blocks: tuple[str, ...] = ()      # keys holding nested step lists
    description: str = ""


def _locator(help_text: str = "e.g. com.example.app:id/button, an XPath, or the visible text") -> tuple[FieldSpec, ...]:
    return (
        FieldSpec("locator_type", "Find element by", "locator_type", default="id", choices=tuple(LOCATOR_TYPES)),
        FieldSpec("locator_value", "Locator value", "text", help=help_text, templated=True),
    )


def _timeout(required: bool = False) -> FieldSpec:
    return FieldSpec("timeout_seconds", "Timeout (seconds)", "float", required=required,
                     default=DEFAULT_TIMEOUT_SECONDS, minimum=0, maximum=600)


def _percent(name: str, label: str, default: float) -> FieldSpec:
    return FieldSpec(name, label, "float", default=default, minimum=0, maximum=100,
                     help="Percent of the screen (0 = left/top, 100 = right/bottom)")


ACTIONS: dict[str, ActionSpec] = {
    spec.key: spec
    for spec in (
        # ---- apps
        ActionSpec("open_app", "Open App", (
            FieldSpec("package", "App package ID", "text", help="e.g. com.whatsapp", templated=True),
        ), group="Apps", description="Launch an app or bring it to the front"),
        ActionSpec("close_app", "Close App", (
            FieldSpec("package", "App package ID", "text", templated=True),
        ), group="Apps", description="Force-stop an app"),
        # ---- elements
        ActionSpec("click", "Click", _locator() + (_timeout(),), group="Elements",
                   description="Tap a button, field or item"),
        ActionSpec("wait_for_element", "Wait For Element", _locator() + (_timeout(required=True),),
                   group="Elements", description="Wait until something appears on screen"),
        ActionSpec("copy_text", "Copy Text", _locator() + (
            FieldSpec("save_as", "Save as (variable name)", "text", default="copied_value",
                      help="Use it later as {{name}} or in a Paste step"),
            _timeout(),
        ), group="Elements", description="Read text from the screen into a variable"),
        ActionSpec("paste_text", "Paste / Type Text", _locator() + (
            FieldSpec("value_from", "Paste variable", "text", required=False, default="copied_value",
                      help="Variable saved by an earlier Copy Text / Set Variable step"),
            FieldSpec("text", "...or type this text", "text", required=False, templated=True,
                      help="Used when no variable is chosen; {{name}} placeholders allowed"),
            _timeout(),
        ), group="Elements", description="Fill an input field"),
        # ---- gestures
        ActionSpec("scroll", "Scroll", (
            FieldSpec("direction", "Direction", "choice", default="down", choices=tuple(DIRECTIONS)),
            FieldSpec("times", "Times", "int", default=1, minimum=1, maximum=100),
        ), group="Gestures", description="Scroll the current screen"),
        ActionSpec("scroll_to_text", "Scroll To Text", (
            FieldSpec("text", "Text to find", "text", templated=True,
                      help="Scrolls the list until an item containing this text is visible"),
        ), group="Gestures", description="Scroll a list until some text is visible"),
        ActionSpec("swipe", "Swipe", (
            _percent("start_x", "Start X (%)", 50), _percent("start_y", "Start Y (%)", 75),
            _percent("end_x", "End X (%)", 50), _percent("end_y", "End Y (%)", 25),
            FieldSpec("duration_ms", "Duration (ms)", "int", default=400, minimum=50, maximum=10000),
        ), group="Gestures", description="Drag a finger across the screen"),
        ActionSpec("tap", "Tap Position", (
            _percent("x", "X (%)", 50), _percent("y", "Y (%)", 50),
        ), group="Gestures", description="Tap a point on the screen"),
        ActionSpec("press_key", "Press Key", (
            FieldSpec("key", "Key", "choice", default="back", choices=tuple(KEYS)),
        ), group="Gestures", description="Back, Home, Enter..."),
        # ---- flow
        ActionSpec("wait", "Wait", (
            FieldSpec("seconds", "Seconds", "float", default=1, minimum=0, maximum=3600),
        ), group="Flow", uses_device=False, description="Pause"),
        ActionSpec("set_variable", "Set Variable", (
            FieldSpec("name", "Variable name", "text", default="my_value"),
            FieldSpec("value", "Value", "text", required=False, templated=True,
                      help="{{name}} placeholders allowed"),
        ), group="Flow", uses_device=False, description="Store a value for later steps"),
        ActionSpec("repeat", "Repeat", (
            FieldSpec("times", "Times", "int", default=2, minimum=1, maximum=10000,
                      help="{{loop_index}} counts 1, 2, 3... inside the loop"),
        ), group="Flow", uses_device=False, blocks=("steps",), description="Run steps several times"),
        ActionSpec("if_exists", "If Element Exists", _locator() + (
            FieldSpec("timeout_seconds", "Look for up to (seconds)", "float", default=3, minimum=0, maximum=600),
        ), group="Flow", blocks=("then", "else"), description="Do different steps depending on the screen"),
        ActionSpec("stop_run", "Stop Script", (
            FieldSpec("message", "Reason", "text", required=False, templated=True),
        ), group="Flow", uses_device=False, description="End the run here"),
        ActionSpec("screenshot", "Take Screenshot", (
            FieldSpec("name", "Name", "text", default="screenshot", templated=True),
        ), group="Flow", description="Save the screen to the run report"),
    )
}

ACTION_LABELS = {key: spec.label for key, spec in ACTIONS.items()}
BLOCK_LABELS = {"steps": "Do", "then": "Then", "else": "Otherwise"}

# Options every device step accepts (shown under "Advanced" in the builder).
DEVICE_FIELD = FieldSpec("device", "Phone", "text", required=False,
                         help="Empty = the default phone. Use A, B… to drive several phones")
RETRIES_FIELD = FieldSpec("retries", "Retries", "int", required=False, default=0, minimum=0, maximum=20,
                          help="Extra attempts before the step counts as failed")
ON_FAIL_FIELD = FieldSpec("on_fail", "If it fails", "choice", required=False, default="skip",
                          choices=tuple(ON_FAIL), help="skip = log and carry on, stop = end the run")
COMMON_FIELDS = (DEVICE_FIELD, RETRIES_FIELD, ON_FAIL_FIELD)

_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class ScriptError(ValueError):
    """Raised when a script file cannot be loaded or is malformed."""


# --------------------------------------------------------------------- helpers


def fields_for(action: str) -> tuple[FieldSpec, ...]:
    """Action fields plus the common options that apply to it."""
    spec = ACTIONS[action]
    if not spec.uses_device:
        return spec.fields
    return spec.fields + COMMON_FIELDS


def render(text: Any, variables: dict[str, Any]) -> str:
    """Replace ``{{name}}`` placeholders; unknown names are left as-is."""
    def substitute(match: re.Match) -> str:
        name = match.group(1)
        return str(variables[name]) if name in variables else match.group(0)
    return _PLACEHOLDER.sub(substitute, str(text))


def placeholders(text: Any) -> list[str]:
    return _PLACEHOLDER.findall(str(text or ""))


def iter_steps(steps: list[dict], path: tuple[int, ...] = ()) -> Iterator[tuple[tuple[int, ...], dict]]:
    """Depth-first walk yielding (path, step); paths are 1-based index tuples."""
    for index, step in enumerate(steps, start=1):
        here = path + (index,)
        yield here, step
        if isinstance(step, dict) and step.get("action") in ACTIONS:
            for key in ACTIONS[step["action"]].blocks:
                children = step.get(key) or []
                if isinstance(children, list):
                    yield from iter_steps(children, here)


def format_path(path: tuple[int, ...]) -> str:
    return ".".join(map(str, path))


def script_roles(script: dict) -> list[str]:
    """Phone roles used by a script, in first-use order."""
    roles: list[str] = []
    for _, step in iter_steps(script.get("steps", [])):
        role = str(step.get("device") or "").strip()
        if role and role not in roles:
            roles.append(role)
    return roles


def is_workflow(script: dict) -> bool:
    """True when a script drives two or more phones together."""
    return len(script_roles(script)) >= 2


def script_variables(script: dict) -> list[str]:
    """Variable names a script defines (copy_text save_as, set_variable name)."""
    names: list[str] = []
    for _, step in iter_steps(script.get("steps", [])):
        name = step.get("save_as") if step.get("action") == "copy_text" else (
            step.get("name") if step.get("action") == "set_variable" else None)
        if name and name not in names:
            names.append(name)
    return names


# --------------------------------------------------------------------- validation


def validate_step(step: dict) -> dict[str, str]:
    """Return ``{field_name: error_message}`` for one step's own fields (not its children).

    The special key ``"action"`` reports a problem with the action itself.
    """
    action = step.get("action")
    if action not in ACTIONS:
        return {"action": f"Unknown action {action!r}"}

    errors: dict[str, str] = {}
    for spec in fields_for(action):
        value = step.get(spec.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if spec.required:
                errors[spec.name] = f"{spec.label} is required"
            continue
        if spec.kind in ("int", "float"):
            if isinstance(value, bool):
                errors[spec.name] = f"{spec.label} must be a number"
                continue
            try:
                number = float(value)
                if spec.kind == "int" and not number.is_integer():
                    raise ValueError
            except (TypeError, ValueError):
                errors[spec.name] = f"{spec.label} must be a {'whole ' if spec.kind == 'int' else ''}number"
                continue
            if spec.minimum is not None and number < spec.minimum:
                errors[spec.name] = f"{spec.label} must be at least {spec.minimum:g}"
            elif spec.maximum is not None and number > spec.maximum:
                errors[spec.name] = f"{spec.label} must be at most {spec.maximum:g}"
        elif spec.kind in ("choice", "locator_type") and value not in spec.choices:
            errors[spec.name] = f"{spec.label} must be one of: {', '.join(spec.choices)}"
        elif spec.name in ("save_as", "value_from") or (action == "set_variable" and spec.name == "name"):
            if not _VARIABLE_NAME.match(str(value)):
                errors[spec.name] = "Use letters, digits and underscores only (no spaces)"

    if action == "paste_text" and not step.get("value_from") and not step.get("text"):
        errors["value_from"] = "Choose a variable to paste, or enter text to type"
    return errors


def validate_steps(steps: Any, path: tuple[int, ...] = (), depth: int = 0) -> list[str]:
    """Problems with a list of steps, including nested blocks."""
    if not isinstance(steps, list):
        return [f"Step {format_path(path)}: block must be a list of steps"]
    if depth > MAX_NESTING:
        return [f"Step {format_path(path)}: blocks are nested too deeply (max {MAX_NESTING})"]
    problems: list[str] = []
    for index, step in enumerate(steps, start=1):
        here = path + (index,)
        label = format_path(here)
        if not isinstance(step, dict):
            problems.append(f"Step {label}: must be an object")
            continue
        for message in validate_step(step).values():
            problems.append(f"Step {label} ({ACTION_LABELS.get(step.get('action'), step.get('action'))}): {message}")
        if step.get("action") in ACTIONS:
            for key in ACTIONS[step["action"]].blocks:
                children = step.get(key, [])
                if key in ("steps", "then") and not children:
                    problems.append(f"Step {label} ({ACTION_LABELS[step['action']]}): "
                                    f"add at least one step under “{BLOCK_LABELS[key]}”")
                problems.extend(validate_steps(children, here, depth + 1))
    return problems


def validate_script(script: dict) -> list[str]:
    """Return human-readable problems with a whole script; empty when valid."""
    if not isinstance(script, dict):
        return ["Script must be a JSON object"]
    problems: list[str] = []
    if not str(script.get("name", "")).strip():
        problems.append("Script needs a name")
    steps = script.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("Script needs at least one step")
        return problems
    problems.extend(validate_steps(steps))
    return problems


# --------------------------------------------------------------------- normalize / describe


def normalize_step(step: dict) -> dict:
    """Drop empty optional fields and defaults, coerce numbers, recurse into blocks."""
    action = step["action"]
    clean: dict[str, Any] = {"action": action}
    for spec in fields_for(action):
        value = step.get(spec.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if spec.kind == "int":
            value = int(float(value))
        elif spec.kind == "float":
            number = float(value)
            value = int(number) if number.is_integer() else number
        elif isinstance(value, str):
            value = value.strip()
        if spec in COMMON_FIELDS and value == spec.default:
            continue  # keep saved files short: retries 0 / on_fail skip are implied
        clean[spec.name] = value
    for key in ACTIONS[action].blocks:
        children = step.get(key) or []
        if children or key != "else":
            clean[key] = [normalize_step(child) for child in children]
    return clean


def describe_step(step: dict) -> str:
    """One-line human summary of a step, used on builder cards and in logs."""
    action = step.get("action")
    label = ACTION_LABELS.get(action, str(action))
    target = f"{step.get('locator_type')}={step.get('locator_value')}"
    if action in ("open_app", "close_app"):
        text = f"{label}: {step.get('package')}"
    elif action == "scroll":
        text = f"{label} {step.get('direction', 'down')} × {step.get('times', 1)}"
    elif action == "scroll_to_text":
        text = f"{label} “{step.get('text')}”"
    elif action == "swipe":
        text = (f"{label} ({step.get('start_x')}%, {step.get('start_y')}%) → "
                f"({step.get('end_x')}%, {step.get('end_y')}%)")
    elif action == "tap":
        text = f"{label} ({step.get('x')}%, {step.get('y')}%)"
    elif action == "press_key":
        text = f"{label}: {step.get('key')}"
    elif action == "wait":
        text = f"{label} {step.get('seconds')}s"
    elif action == "set_variable":
        text = f"{label} {step.get('name')} = “{step.get('value', '')}”"
    elif action == "repeat":
        text = f"{label} {step.get('times')} times"
    elif action == "if_exists":
        text = f"If {target} is on screen"
    elif action == "stop_run":
        text = f"{label}" + (f": {step['message']}" if step.get("message") else "")
    elif action == "screenshot":
        text = f"{label} “{step.get('name')}”"
    elif action == "copy_text":
        text = f"{label} from {target} → {step.get('save_as')}"
    elif action == "paste_text":
        source = f"{{{{{step['value_from']}}}}}" if step.get("value_from") else f"“{step.get('text', '')}”"
        text = f"{label} {source} into {target}"
    elif action == "wait_for_element":
        from .settings import default_timeout

        text = f"{label} {target} (up to {step.get('timeout_seconds', default_timeout()):g}s)"
    else:
        text = f"{label} {target}"
    extras = []
    if step.get("retries"):
        extras.append(f"{step['retries']} retries")
    if step.get("on_fail") == "stop":
        extras.append("stop on failure")
    return text + (f"  [{', '.join(extras)}]" if extras else "")


# --------------------------------------------------------------------- files


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
    if script.get("description"):
        data["description"] = str(script["description"]).strip()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_filename(data['name'])}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
