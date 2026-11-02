from ninja import avatar_approval, state_machine


def test_tahuti_approval_has_no_deadline(monkeypatch):
    calls = []

    class FakeMachine:
        payload = None

        def set_state(self, *args):
            pass

    class FakeWindow:
        def get_visible(self):
            return True

    class FakeOverlay:
        machine = FakeMachine()
        _win = FakeWindow()

        def set_agent(self, agent):
            pass

        def set_card_text(self, text):
            pass

        def present_approval(self, *, timeout):
            calls.append(timeout)
            return "deny"

        def park_idle(self):
            pass

    monkeypatch.setattr(avatar_approval, "_GTK_AVAILABLE", True)
    monkeypatch.setattr(avatar_approval, "get_overlay", lambda **kwargs: FakeOverlay())
    assert avatar_approval.ask_avatar_approval("card", agent="tahuti") == "deny"
    assert calls == [None]


def test_non_tahuti_timeout_remains_120_seconds(monkeypatch):
    calls = []

    class Machine:
        payload = None

        def set_state(self, *args):
            pass

    class Window:
        def get_visible(self):
            return True

    class Overlay:
        machine = Machine()
        _win = Window()

        def set_agent(self, agent):
            pass

        def set_card_text(self, text):
            pass

        def present_approval(self, *, timeout):
            calls.append(timeout)
            return "deny"

        def park_idle(self):
            pass

    monkeypatch.setattr(avatar_approval, "_GTK_AVAILABLE", True)
    monkeypatch.setattr(avatar_approval, "get_overlay", lambda **kwargs: Overlay())
    avatar_approval.ask_avatar_approval("card", agent="ninja")
    assert calls == [120]


def test_close_event_fails_closed():
    class Loop:
        def __init__(self):
            self.quit_called = False

        def is_running(self):
            return True

        def quit(self):
            self.quit_called = True

    obj = object.__new__(state_machine.ThootieOverlay)
    obj._approval_verdict = None
    obj._approval_loop = Loop()
    assert obj._on_window_delete(None, None) is True
    assert obj._approval_verdict == "deny"
    assert obj._approval_loop.quit_called


def test_card_widget_is_in_scrolled_window():
    assert (
        "self._card_scroll = Gtk.ScrolledWindow()"
        in open(state_machine.__file__).read()
    )
    assert (
        "self._card_scroll.add(self._card_label)" in open(state_machine.__file__).read()
    )
