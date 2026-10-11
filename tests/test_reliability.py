"""Click checks, pop-up closing, reconnecting a dropped phone and silent typing."""

import io

import pytest

from core import errorlog, reliability, runner as runner_module
from core.devices import DeviceError, DeviceSession
from core.errorlog import ErrorEntry
from core.runner import RunState, ScriptRunner
from tests.fakes import FakeSession

CLICK = {"name": "t", "steps": [{"action": "click", "locator_type": "id", "locator_value": "go",
                                 "timeout_seconds": 3, "on_fail": "stop"}]}


def _xml(*nodes: str) -> str:
    return ("<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation='0'>"
            "<node class='android.widget.FrameLayout' bounds='[0,0][1080,2400]' package='com.app'>"
            + "".join(nodes) + "</node></hierarchy>")


def _node(text="", rid="", package="com.app", bounds="[100,100][300,200]", clickable="true", **extra):
    attrs = " ".join(f'{k}="{v}"' for k, v in extra.items())
    return (f'<node class="android.widget.Button" text="{text}" resource-id="{rid}" package="{package}" '
            f'bounds="{bounds}" clickable="{clickable}" content-desc="" {attrs}/>')


def _png(mark: bool) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1080, 2400), (250, 250, 250))
    if mark:  # a small icon changing colour, like a "like" heart
        ImageDraw.Draw(image).ellipse((500, 1500, 572, 1572), fill=(220, 30, 60))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


# --------------------------------------------------------------------- pop-ups


ANR = _xml(_node("Shop isn't responding", "android:id/alertTitle", "android", clickable="false"),
           _node("Close app", "android:id/aerr_close", "android", "[100,900][980,1000]"),
           _node("Wait", "android:id/aerr_wait", "android", "[100,1000][980,1100]"))
PERMISSION = _xml(_node("Allow Shop to take pictures?", "com.android.permissioncontroller:id/permission_message",
                        "com.android.permissioncontroller", clickable="false"),
                  _node("While using the app",
                        "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
                        "com.android.permissioncontroller", "[100,1200][980,1300]"),
                  _node("Don't allow", "com.android.permissioncontroller:id/permission_deny_button",
                        "com.android.permissioncontroller", "[100,1400][980,1500]"))


def test_find_popup_recognises_blocking_dialogs():
    popup = reliability.find_popup(ANR)
    assert popup.button == "Wait" and popup.point == (540, 1050) and "isn't responding" in popup.what

    assert reliability.find_popup(PERMISSION) is None  # permission prompts are left alone by default
    allow = reliability.find_popup(PERMISSION, permissions="allow")
    assert allow.button == "While using the app" and allow.point == (540, 1250)
    assert reliability.find_popup(PERMISSION, permissions="deny").button == "Don't allow"

    shade = _xml(_node(rid="com.android.systemui:id/notification_stack_scroller", package="com.android.systemui"))
    assert reliability.find_popup(shade).point is None  # closed with Back

    notice = _xml(_node("Battery low", "android:id/alertTitle", "com.android.systemui", clickable="false"),
                  _node("OK", "android:id/button1", "com.android.systemui", "[600,1300][900,1400]"),
                  _node("Turn on Battery Saver", "android:id/button2", "com.android.systemui"))
    assert reliability.find_popup(notice).button == "OK"

    rating = _xml(_node("Enjoying Shop?"), _node("Not now", bounds="[0,0][200,100]"))
    assert reliability.find_popup(rating) is None  # an app's own dialog: only with the user's list
    assert reliability.find_popup(rating, extra_buttons=("not now",)).point == (100, 50)
    assert reliability.find_popup(_xml(_node("Pay now"))) is None


def test_layout_and_pixel_change_detection():
    a = _xml(_node("Like", selected="false"))
    b = _xml(_node("Like", selected="true"))
    assert reliability.layout_signature(a) == reliability.layout_signature(_xml(_node("Like", selected="false")))
    assert reliability.layout_signature(a) != reliability.layout_signature(b)
    assert reliability.pixels_changed(_png(False), _png(True))
    assert not reliability.pixels_changed(_png(False), _png(False))


# --------------------------------------------------------------------- the runner


class ScreenSession(FakeSession):
    """A fake phone whose layout and picture can change when it is tapped."""

    def __init__(self, serial="S1", on_tap=None):
        super().__init__(serial, {"id=go": ""})
        self.layout = _xml(_node("Go", "com.app:id/go"))
        self.picture = _png(False)
        self.on_tap = on_tap or (lambda session: None)
        self.taps = 0

    def begin_pass(self):
        self.last_source = self.layout

    def page_source(self):
        self._require_driver()
        return self.layout

    def quick_screenshot(self):
        return self.picture

    def click_candidate(self, candidate):
        super().click_candidate(candidate)
        self.taps += 1
        self.on_tap(self)

    def tap(self, x, y):
        super().tap(x, y)
        self.on_tap(self)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(runner_module, "REACTION_SECONDS", 0.2)
    monkeypatch.setattr(runner_module, "REACTION_POLL", 0.05)
    monkeypatch.setattr(runner_module, "POPUP_CHECK_SECONDS", 0.0)


def _run(session, script=CLICK):
    errors: list[ErrorEntry] = []
    result = ScriptRunner(session, script, on_error=errors.append).run()
    return result, errors


def test_click_that_changes_the_screen_is_tapped_once(fast):
    def navigate(session):
        session.layout = _xml(_node("Next page"))

    session = ScreenSession(on_tap=navigate)
    result, errors = _run(session)
    assert result.state == RunState.COMPLETED and session.taps == 1 and not errors


def test_icon_only_change_counts_as_a_reaction(fast):
    def like(session):
        session.picture = _png(True)  # same layout, the heart turns red

    session = ScreenSession(on_tap=like)
    result, _ = _run(session)
    assert result.state == RunState.COMPLETED and session.taps == 1  # never tapped twice (no "unlike")


def test_lost_tap_is_repeated_once(fast):
    def second_tap_works(session):
        if session.taps == 2:
            session.layout = _xml(_node("Next page"))

    session = ScreenSession(on_tap=second_tap_works)
    result, errors = _run(session)
    assert result.state == RunState.COMPLETED and session.taps == 2 and not errors


def test_no_reaction_continues_or_fails_as_the_step_says(fast):
    session = ScreenSession()
    result, errors = _run(session)
    assert result.state == RunState.COMPLETED and session.taps == 2
    assert [e.kind for e in errors] == [errorlog.NO_REACTION]

    strict = {"name": "t", "steps": [dict(CLICK["steps"][0], no_reaction="fail")]}
    session = ScreenSession()
    result, errors = _run(session, strict)
    assert result.state == RunState.FAILED and "didn't react" in result.error


def test_click_check_can_be_turned_off(fast, monkeypatch):
    from core import settings as app_settings

    monkeypatch.setattr(app_settings, "_current", app_settings.Settings(verify_clicks=False))
    session = ScreenSession()
    result, _ = _run(session)
    assert result.state == RunState.COMPLETED and session.taps == 1


def test_blocking_popup_is_closed_and_the_step_goes_on(fast):
    session = ScreenSession()
    session.screen = {}  # the button is hidden behind the dialog
    session.layout = ANR

    def tapped(session):
        if session.layout == ANR and session.calls[-1] == ("tap", 540, 1050):
            session.layout = _xml(_node("Go", "com.app:id/go"))
            session.screen["id=go"] = ""
        elif session.layout != ANR:
            session.layout = _xml(_node("Next page"))

    session.on_tap = tapped
    result, errors = _run(session)
    assert result.state == RunState.COMPLETED
    assert ("tap", 540, 1050) in session.calls and ("click", "id=go") in session.calls
    assert errors[0].kind == errorlog.POPUP and "Wait" in errors[0].reason


def test_dropped_phone_is_reconnected_and_the_step_repeated(fast):
    class Flaky(ScreenSession):
        drops = 1
        recovered = 0

        def click_candidate(self, candidate):
            if self.drops:
                self.drops -= 1
                raise DeviceError("Cannot be proxied to UiAutomator2 server because the instrumentation "
                                  "process is not running (probably crashed)")
            super().click_candidate(candidate)

        def recover(self, timeout=0, on_progress=None):
            self.recovered += 1
            on_progress("phone is back")
            return True

    session = Flaky(on_tap=lambda s: setattr(s, "layout", _xml(_node("Next page"))))
    result, errors = _run(session)
    assert result.state == RunState.COMPLETED and session.recovered == 1 and session.taps == 1
    assert errors[0].kind == errorlog.RECONNECTED


def test_phone_that_stays_gone_still_fails(fast):
    class Gone(ScreenSession):
        def click_candidate(self, candidate):
            raise DeviceError("device 'S1' not found")

    result, _ = _run(Gone())
    assert result.state == RunState.FAILED and "not found" in result.error


def test_wait_for_device_helps_the_phone_back(monkeypatch):
    calls = []
    states = iter(["List of devices attached\n192.168.1.5:5555\toffline\n",
                   "List of devices attached\n192.168.1.5:5555\tdevice\n"])

    def run_adb(*args, serial=None, timeout=None):
        calls.append(args)
        return next(states) if args == ("devices",) else ""

    monkeypatch.setattr(reliability, "run_adb", run_adb)
    monkeypatch.setattr(reliability.time, "sleep", lambda s: None)
    assert reliability.wait_for_device("192.168.1.5:5555", timeout=30)
    assert ("connect", "192.168.1.5:5555") in calls

    def hung(*args, serial=None, timeout=None):
        calls.append(args)
        raise DeviceError("adb devices timed out")

    calls.clear()
    monkeypatch.setattr(reliability, "run_adb", hung)
    monkeypatch.setattr(reliability, "_last_adb_restart", None)
    clock = iter(range(0, 1000, 2))
    monkeypatch.setattr(reliability.time, "monotonic", lambda: next(clock))
    assert not reliability.wait_for_device("S1", timeout=10)
    assert calls.count(("kill-server",)) == 1  # restarted once, not every few seconds


# --------------------------------------------------------------------- silent typing


class FakeElement:
    def __init__(self, accepts=("direct", "keys", "paste"), password=False):
        self.id = "el1"
        self.text = ""
        self.accepts = set(accepts)
        self.password = password
        self.clicks = 0

    def get_attribute(self, name):
        return ("true" if self.password else "false") if name == "password" else self.text

    def click(self):
        self.clicks += 1

    def clear(self):
        self.text = ""

    def send_keys(self, text):
        if "keys" in self.accepts:
            self.text = text


class FakeDriver:
    def __init__(self, element):
        self.element = element
        self.clipboard = ""
        self.keyboard_hidden = 0

    def execute_script(self, name, args):
        if "direct" not in self.element.accepts:
            raise RuntimeError("replaceElementValue is not supported here")
        self.element.text = args["text"]

    def set_clipboard_text(self, text):
        self.clipboard = text

    def press_keycode(self, code):
        if code == DeviceSession.PASTE_KEYCODE and "paste" in self.element.accepts:
            self.element.text = self.clipboard

    def is_keyboard_shown(self):
        return True

    def hide_keyboard(self):
        self.keyboard_hidden += 1


@pytest.mark.parametrize("accepts, expected, clicks", [
    (("direct", "keys", "paste"), "silent", 0),   # no tap on the field: the keyboard never opens
    (("keys", "paste"), "silent", 0),
    (("paste",), "paste", 1),
    ((), "keyboard", 2),
])
def test_silent_typing_falls_back_in_order(accepts, expected, clicks):
    session = DeviceSession("S1", connect=False)
    element = FakeElement(accepts)
    session.driver = FakeDriver(element)
    assert session._type(element, "سلام دنیا 👋") == expected
    assert element.clicks == clicks
    if expected != "keyboard":
        assert element.text == "سلام دنیا 👋"


def test_silent_typing_trusts_password_fields_and_formatted_numbers():
    session = DeviceSession("S1", connect=False)
    secret = FakeElement(("direct",), password=True)
    session.driver = FakeDriver(secret)
    assert session._type(secret, "hunter2") == "silent"

    class Formatting(FakeElement):
        def get_attribute(self, name):
            return "0300 123-4567" if name == "text" else "false"

    number = Formatting(("direct",))
    session.driver = FakeDriver(number)
    assert session._type(number, "03001234567") == "silent"


def test_popup_that_will_not_close_does_not_hold_the_step_forever(fast):
    import time

    session = ScreenSession()
    session.screen = {}
    session.layout = ANR  # tapping Wait changes nothing
    started = time.monotonic()
    result, errors = _run(session)
    assert result.state == RunState.FAILED and time.monotonic() - started < 30
    assert len([e for e in errors if e.kind == errorlog.POPUP]) == runner_module.MAX_POPUPS_PER_STEP


# --------------------------------------------------------------------- app guard


class WanderingSession(FakeSession):
    """A phone that has wandered into another app; Open App brings the app back."""

    def __init__(self, now_in="com.launcher"):
        super().__init__("S1", {})
        self.package = now_in

    def open_app(self, package):
        super().open_app(package)
        self.package = package
        self.screen["id=go"] = ""  # the app's button is visible again


@pytest.fixture
def guard_fast(fast, monkeypatch):
    monkeypatch.setattr(runner_module, "GUARD_AFTER_SECONDS", 0.0)
    monkeypatch.setattr(runner_module, "GUARD_CHECK_SECONDS", 0.0)


def _guarded_click(**extra):
    return {"name": "t", "steps": [dict(CLICK["steps"][0], **extra)]}


def test_app_guard_brings_the_picked_app_back(guard_fast):
    session = WanderingSession()
    result, errors = _run(session, _guarded_click(screen={"package": "com.shop", "anchors": []}))
    assert result.state == RunState.COMPLETED
    assert ("open_app", "com.shop") in session.calls and ("click", "id=go") in session.calls
    assert errors[0].kind == errorlog.APP_RETURNED and "com.launcher" in errors[0].reason


def test_app_guard_uses_the_last_open_app_and_confirms_twice(guard_fast):
    session = WanderingSession(now_in="com.shop")
    script = {"name": "t", "steps": [{"action": "open_app", "package": "com.shop"},
                                     {"action": "press_key", "key": "home"},
                                     dict(CLICK["steps"][0])]}

    def go_home(key):
        session.package = "com.launcher"
        session.screen.pop("id=go", None)  # the app's button isn't on the home screen

    session.press_key = go_home
    result, errors = _run(session, script)
    assert result.state == RunState.COMPLETED
    assert session.calls.count(("open_app", "com.shop")) == 2  # the step's own + the guard's
    assert [e.kind for e in errors] == [errorlog.APP_RETURNED]


def test_app_guard_leaves_dialogs_and_can_be_turned_off(guard_fast, monkeypatch):
    from core import settings as app_settings

    session = WanderingSession(now_in="android")  # a system dialog over the app: not "another app"
    result, _ = _run(session, _guarded_click(screen={"package": "com.shop", "anchors": []}, timeout_seconds=0.5))
    assert result.state == RunState.FAILED and ("open_app", "com.shop") not in session.calls

    monkeypatch.setattr(app_settings, "_current", app_settings.Settings(app_guard=False))
    session = WanderingSession()
    result, _ = _run(session, _guarded_click(screen={"package": "com.shop", "anchors": []}, timeout_seconds=0.5))
    assert result.state == RunState.FAILED and ("open_app", "com.shop") not in session.calls
