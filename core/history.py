"""Run history: every run gets a folder under ``runs/`` with a JSON + HTML report.

    runs/20261009-153012_R58M123ABC_Copy_order/
        report.json        machine-readable record (used by the History tab)
        report.html        self-contained report to open or share
        screenshots/*.png  failure screenshots and Take Screenshot steps
"""

from __future__ import annotations

import csv
import html
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

REPORT_JSON = "report.json"
REPORT_HTML = "report.html"


@dataclass
class StepRecord:
    path: str
    summary: str
    device: str
    status: str            # "ok", "skipped", "failed", "info"
    started_at: float
    duration: float = 0.0
    attempts: int = 1
    error: str = ""
    screenshot: str = ""   # relative path inside the run folder


@dataclass
class RunRecord:
    run_id: str
    script: str
    devices: dict[str, str]   # role (or "device") -> serial
    run_number: int
    trigger: str              # "manual", "schedule: <name>", "cli"
    started_at: float
    finished_at: float | None = None
    state: str = "running"
    message: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def counts(self) -> dict[str, int]:
        result = {"ok": 0, "skipped": 0, "failed": 0}
        for step in self.steps:
            if step.status in result:
                result[step.status] += 1
        return result


def _slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:limit] or "run"


class RunRecorder:
    """Collects step results for one run and writes the report when it finishes.

    Thread-safe: a cross-phone workflow records from one thread, but the UI may
    read ``record`` while it is running.
    """

    def __init__(self, script: str, devices: dict[str, str], run_number: int = 1,
                 trigger: str = "manual", base_dir: Path | None = None):
        started = time.time()
        stamp = datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S")
        serials = "+".join(_slug(s, 20) for s in devices.values())
        base = Path(base_dir or paths.runs_dir())
        folder = base / f"{stamp}_{serials}_{_slug(script)}"
        suffix = 1
        while folder.exists():
            suffix += 1
            folder = base / f"{stamp}_{serials}_{_slug(script)}_{suffix}"
        self.folder = folder
        self.record = RunRecord(run_id=folder.name, script=script, devices=dict(devices),
                                run_number=run_number, trigger=trigger, started_at=started)
        self._lock = threading.Lock()
        self._shots = 0

    def save_screenshot(self, png: bytes | None, name: str) -> str:
        """Write a screenshot into the run folder; returns its relative path ('' on failure)."""
        if not png:
            return ""
        with self._lock:
            self._shots += 1
            relative = f"screenshots/{self._shots:03d}_{_slug(name, 30)}.png"
        try:
            target = self.folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
            return relative
        except OSError:
            log.exception("Could not save screenshot")
            return ""

    def add_step(self, step: StepRecord) -> None:
        with self._lock:
            self.record.steps.append(step)

    def finish(self, state: str, message: str = "", variables: dict | None = None) -> Path:
        with self._lock:
            self.record.state = state
            self.record.message = message
            self.record.finished_at = time.time()
            self.record.variables = {k: _jsonable(v) for k, v in (variables or {}).items()}
            data = asdict(self.record)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / REPORT_JSON).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            (self.folder / REPORT_HTML).write_text(render_html(self.record), encoding="utf-8")
        except OSError:
            log.exception("Could not write run report")
        return self.folder


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------- reading


def load_run(folder: Path) -> RunRecord | None:
    try:
        data = json.loads((folder / REPORT_JSON).read_text(encoding="utf-8"))
        steps = [StepRecord(**s) for s in data.pop("steps", [])]
        return RunRecord(**data, steps=steps)
    except (OSError, ValueError, TypeError):
        return None


def list_runs(base_dir: Path | None = None, limit: int = 500) -> list[tuple[Path, RunRecord]]:
    """Most recent runs first."""
    base = Path(base_dir or paths.runs_dir())
    if not base.exists():
        return []
    folders = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    runs = []
    for folder in folders:
        record = load_run(folder)
        if record:
            runs.append((folder, record))
        if len(runs) >= limit:
            break
    return runs


def export_csv(runs: list[tuple[Path, RunRecord]], target: Path) -> None:
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["started", "script", "devices", "run", "trigger", "result", "message",
                         "duration_s", "ok", "skipped", "failed", "report"])
        for folder, run in runs:
            counts = run.counts()
            writer.writerow([
                datetime.fromtimestamp(run.started_at).isoformat(timespec="seconds"), run.script,
                " ".join(f"{k}={v}" for k, v in run.devices.items()), run.run_number, run.trigger,
                run.state, run.message, f"{run.duration:.1f}", counts["ok"], counts["skipped"],
                counts["failed"], str(folder / REPORT_HTML),
            ])


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# --------------------------------------------------------------------- HTML


_STATE_COLORS = {"completed": "#16a34a", "partial": "#d97706", "failed": "#dc2626",
                 "stopped": "#6b7280", "running": "#2563eb"}
_STEP_COLORS = {"ok": "#16a34a", "skipped": "#d97706", "failed": "#dc2626", "info": "#6b7280"}


def render_html(run: RunRecord) -> str:
    e = html.escape
    counts = run.counts()
    started = datetime.fromtimestamp(run.started_at).strftime("%Y-%m-%d %H:%M:%S")
    color = _STATE_COLORS.get(run.state, "#6b7280")
    devices = ", ".join(f"{e(role)} → {e(serial)}" if role not in (serial, "device") else e(serial)
                        for role, serial in run.devices.items())
    rows = []
    for step in run.steps:
        shot = (f'<a href="{e(step.screenshot)}"><img src="{e(step.screenshot)}" alt="screenshot"></a>'
                if step.screenshot else "")
        attempts = f" <span class=muted>({step.attempts} attempts)</span>" if step.attempts > 1 else ""
        rows.append(
            f"<tr><td class=mono>{e(step.path)}</td>"
            f"<td><span class=pill style='background:{_STEP_COLORS.get(step.status, '#6b7280')}'>"
            f"{e(step.status)}</span></td>"
            f"<td>{e(step.summary)}{attempts}"
            + (f"<div class=error>{e(step.error)}</div>" if step.error else "")
            + f"</td><td class=mono>{e(step.device)}</td><td class=mono>{step.duration:.1f}s</td><td>{shot}</td></tr>")
    variables = "".join(f"<tr><td class=mono>{e(str(k))}</td><td>{e(str(v))}</td></tr>"
                        for k, v in run.variables.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run report – {e(run.script)}</title>
<style>
:root {{ --bg:#f7f7f8; --card:#fff; --text:#18181b; --muted:#6b7280; --border:#e4e4e7; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#15171c; --card:#1d2027; --text:#e6e8ec; --muted:#9aa1ad; --border:#2f3440; }}
}}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 "Segoe UI",system-ui,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.muted {{ color:var(--muted); }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; margin-top:16px; }}
.summary {{ display:flex; flex-wrap:wrap; gap:24px; }}
.summary div b {{ display:block; font-size:18px; }}
.pill {{ color:#fff; border-radius:999px; padding:1px 9px; font-size:12px; font-weight:600; }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; }}
.mono {{ font-family:Consolas,"Cascadia Mono",monospace; font-size:12.5px; }}
.error {{ color:#dc2626; font-size:12.5px; margin-top:2px; }}
img {{ max-width:160px; max-height:280px; border:1px solid var(--border); border-radius:6px; }}
.scroll {{ overflow-x:auto; }}
</style></head><body><main>
<h1>{e(run.script)}</h1>
<div class="muted">{e(started)} · {devices} · run #{run.run_number} · {e(run.trigger)}</div>
<div class="card summary">
  <div><span class="muted">Result</span><b style="color:{color}">{e(run.state.title())}</b></div>
  <div><span class="muted">Duration</span><b>{format_duration(run.duration)}</b></div>
  <div><span class="muted">Steps OK</span><b>{counts['ok']}</b></div>
  <div><span class="muted">Skipped</span><b>{counts['skipped']}</b></div>
  <div><span class="muted">Failed</span><b>{counts['failed']}</b></div>
</div>
{f'<div class="card">{e(run.message)}</div>' if run.message else ''}
<div class="card scroll"><table>
<thead><tr><th>Step</th><th>Status</th><th>What</th><th>Phone</th><th>Time</th><th>Screenshot</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=6 class=muted>No steps ran.</td></tr>'}</tbody>
</table></div>
{f'<div class="card scroll"><table><thead><tr><th>Variable</th><th>Value</th></tr></thead><tbody>{variables}</tbody></table></div>' if variables else ''}
</main></body></html>
"""
