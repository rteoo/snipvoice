"""Page message slots for a ``MeetingWindow`` built with ``__new__`` in a unit test."""

import types

from meeting_gui import MeetingWindow, Messages


def give_messages(view, page="library"):
    """Give a hand-built view the message slots a real window owns, drawn nowhere.

    ``page`` is the destination the user is looking at, which is where an
    outcome with no page of its own is written.
    """
    tabs = {"recording": ".recording", "library": ".library", "settings": ".settings"}
    view.recording_tab, view.library_tab, view.settings_tab = tabs.values()
    view.notebook = types.SimpleNamespace(select=lambda: tabs[view._destination])
    view.message_bars = {}
    view.closed = getattr(view, "closed", False)
    view._destination = page
    view.messages = Messages(view._render_message)
    return view


def new_view(page="library"):
    """A bare ``MeetingWindow`` with message slots."""
    return give_messages(MeetingWindow.__new__(MeetingWindow), page)


def said(view, page="library"):
    """``(kind, title, body, details)`` of the message a page shows, or ``None``."""
    message = view.messages.current(page)
    return None if message is None else (message.kind, message.title, message.body, message.details)
