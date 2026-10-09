"""Dark professional theme shared by every window."""

from __future__ import annotations

from core.runner import RunState

BG = "#15171c"
SURFACE = "#1d2027"
SURFACE_ALT = "#242833"
BORDER = "#2f3440"
TEXT = "#e6e8ec"
TEXT_MUTED = "#9aa1ad"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2f6fd8"
DANGER = "#ef4444"

STATUS_COLORS = {
    RunState.IDLE: "#7b818c",
    RunState.CONNECTING: "#a78bfa",
    RunState.RUNNING: "#3b82f6",
    RunState.WAITING: "#38bdf8",
    RunState.COMPLETED: "#22c55e",
    RunState.PARTIAL: "#f59e0b",
    RunState.FAILED: "#ef4444",
    RunState.STOPPED: "#a1a1aa",
    "disconnected": "#4b5060",
}

STATUS_LABELS = {
    RunState.IDLE: "Idle",
    RunState.CONNECTING: "Connecting…",
    RunState.RUNNING: "Running",
    RunState.WAITING: "Waiting for next run",
    RunState.COMPLETED: "Done",
    RunState.PARTIAL: "Done (some steps skipped)",
    RunState.FAILED: "Failed",
    RunState.STOPPED: "Stopped",
    "disconnected": "Disconnected",
}

STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
    font-size: 10pt;
}}
QMainWindow::separator {{ background: {BORDER}; width: 1px; }}
QToolBar {{
    background: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 10px;
    spacing: 6px;
}}
QToolBar QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 12px;
}}
QToolBar QToolButton:hover {{ background: {SURFACE_ALT}; border-color: {BORDER}; }}
QStatusBar {{ background: {SURFACE}; color: {TEXT_MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ background: transparent; color: {TEXT_MUTED}; }}

QFrame#Panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QLabel#PanelTitle {{ background: transparent; font-size: 11pt; font-weight: 600; }}
QLabel#Muted {{ background: transparent; color: {TEXT_MUTED}; }}
QLabel#Error {{ background: transparent; color: {DANGER}; font-size: 9pt; }}
QLabel#EmptyState {{ background: transparent; color: {TEXT_MUTED}; padding: 24px; }}

QFrame#DeviceRow, QFrame#StepCard {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame#DeviceRow[selected="true"] {{ border: 1px solid {ACCENT}; }}
QFrame#DeviceRow QLabel, QFrame#StepCard QLabel {{ background: transparent; }}
QLabel#DeviceName {{ font-weight: 600; font-size: 10.5pt; }}

QPushButton {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {TEXT_MUTED}; }}
QPushButton:disabled {{ color: #5c6270; border-color: #2a2e38; }}
QPushButton#Primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#Primary:disabled {{ background: #2a3a55; border-color: #2a3a55; color: #8794aa; }}
QPushButton#Danger {{ color: {DANGER}; }}
QPushButton#Danger:disabled {{ color: #5c6270; }}
QPushButton#Icon {{ padding: 4px 8px; min-width: 22px; }}

QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QLineEdit[invalid="true"] {{ border-color: {DANGER}; }}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}
QPlainTextEdit, QListWidget {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px;
    selection-background-color: {ACCENT};
}}
QPlainTextEdit#Log, QPlainTextEdit#Code {{
    font-family: "Cascadia Mono", "Consolas", "Courier New", "DejaVu Sans Mono", monospace;
    font-size: 9.5pt;
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; background: {SURFACE}; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 8px 18px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QSplitter::handle {{ background: transparent; width: 10px; }}
QFrame#StepCard[depth="1"] {{ background: #20242e; }}
QFrame#StepCard[depth="2"] {{ background: #262b36; }}
QLabel#Badge {{
    background: #24324a; color: #93c5fd; border-radius: 8px; padding: 1px 8px; font-size: 8.5pt; font-weight: 600;
}}
QLabel#BlockTitle {{ background: transparent; color: {TEXT_MUTED}; font-weight: 600; font-size: 9pt; }}
QPushButton#Ghost {{ background: transparent; border: 1px dashed {BORDER}; color: {TEXT_MUTED}; padding: 4px 12px; }}
QPushButton#Ghost:hover {{ color: {TEXT}; border-color: {TEXT_MUTED}; }}
QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding: 12px 8px 6px 8px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {TEXT_MUTED}; }}
QTableWidget {{
    background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; gridline-color: {BORDER};
    selection-background-color: #24324a; selection-color: {TEXT};
}}
QTableWidget::item {{ padding: 4px 8px; }}
QHeaderView::section {{
    background: {SURFACE}; color: {TEXT_MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 6px 8px; font-weight: 600;
}}
QTimeEdit {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px; }}
QCheckBox {{ background: transparent; spacing: 6px; }}
QCheckBox::indicator, QTableWidget::indicator {{
    width: 15px; height: 15px; border: 1px solid {TEXT_MUTED}; border-radius: 4px; background: {BG};
}}
QCheckBox::indicator:checked, QTableWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; min-width: 30px; }}
QToolTip {{ background: {SURFACE_ALT}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
"""


def status_dot_style(state: str) -> str:
    color = STATUS_COLORS.get(state, STATUS_COLORS[RunState.IDLE])
    return f"background: {color}; border-radius: 6px; min-width: 12px; max-width: 12px; min-height: 12px; max-height: 12px;"
