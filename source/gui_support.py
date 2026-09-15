import platform_support

from rich_text_support import extract_plain_text, is_rich_text_payload
from variable_support import find_variable_names

PREVIEW_CHARS = 60


def snippet_row_values(key, value, preview_chars=PREVIEW_CHARS):
    """Return ``(trigger, preview, markers)`` for one Treeview row.

    Read-only: snippet values are user data and are never mutated here.
    """
    plain = extract_plain_text(value)
    preview = " ".join(plain.split())
    if len(preview) > preview_chars:
        preview = preview[: preview_chars - 1].rstrip() + "…"

    markers = []
    if is_rich_text_payload(value):
        markers.append("RT")
    if find_variable_names(plain):
        markers.append("%%")
    return key, preview, " ".join(markers)


def filter_static_snippets(snippets, query):
    lowered = query.strip().lower()
    visible = {
        key: value
        for key, value in snippets.items()
        if (not key.startswith("_")) and (not callable(value))
    }
    if not lowered:
        return visible
    return {
        key: value
        for key, value in visible.items()
        if lowered in key.lower() or lowered in extract_plain_text(value).lower()
    }


def iter_filtered_mapping_items(mapping, query):
    lowered = query.strip().lower()
    if not isinstance(mapping, dict):
        return []
    items = []
    for key in sorted(mapping.keys()):
        if key == "__prefix__":
            continue
        value = extract_plain_text(mapping.get(key, ""))
        if lowered and lowered not in key.lower() and lowered not in value.lower():
            continue
        items.append(key)
    return items


def center_on_screen(dialog, vertical_divisor=2):
    """Center a dialog on the screen.

    Used by dialogs whose parent is the hidden shared root, which has no
    meaningful geometry to center against.
    """
    dialog.update_idletasks()
    width = dialog.winfo_reqwidth()
    height = dialog.winfo_reqheight()
    x = (dialog.winfo_screenwidth() - width) // 2
    y = (dialog.winfo_screenheight() - height) // vertical_divisor
    dialog.geometry(f"{width}x{height}+{x}+{y}")


def center_dialog(dialog, root):
    dialog.update_idletasks()
    width = dialog.winfo_reqwidth()
    height = dialog.winfo_reqheight()
    x = root.winfo_rootx() + (root.winfo_width() - width) // 2
    y = root.winfo_rooty() + (root.winfo_height() - height) // 2
    dialog.geometry(f"{width}x{height}+{x}+{y}")


def focus_modal_input(dialog, initial_widget, submit):
    """Reveal a withdrawn modal dialog only after its app can receive keys.

    ``submit`` is ``GuiThread.submit``. The native activation callback may run
    inside Cocoa, so it queues the reveal/focus closure instead of touching Tk.
    Callers must withdraw the Toplevel immediately after creating it and invoke
    the returned cancellation callable after ``wait_window`` unwinds.
    """
    state = {"cancelled": False}

    def fail(reason):
        if state["cancelled"]:
            return
        state["failure"] = reason
        submit(lambda _root: dialog.destroy())

    def show_ready(_root):
        if state["cancelled"]:
            return
        if platform_support.IS_MAC:
            dialog.attributes("-alpha", 1.0)
        dialog.lift()
        focus_target = initial_widget if initial_widget is not None else dialog
        focus_target.focus_force()

    def request_key_focus(_root):
        if state["cancelled"]:
            return
        focus_target = initial_widget if initial_widget is not None else dialog
        state["cancel_key_focus"] = platform_support.focus_tk_window_when_ready(
            dialog,
            focus_target,
            lambda: submit(show_ready),
            fail,
        )

    def queue_key_focus():
        submit(request_key_focus)

    state["failure"] = None
    state["cancel_key_focus"] = lambda: None
    # Give AppKit a real window to activate, but do not let it look typeable
    # until both the application and this exact NSWindow own keyboard focus.
    if platform_support.IS_MAC:
        dialog.attributes("-alpha", 0.0)
    dialog.deiconify()
    dialog.wait_visibility()
    dialog.lift()
    cancel_activation = platform_support.activate_application_when_ready(
        queue_key_focus,
        fail,
    )

    def cancel():
        state["cancelled"] = True
        cancel_activation()
        state["cancel_key_focus"]()
        if state["failure"] is not None:
            raise RuntimeError(state["failure"])

    return cancel
