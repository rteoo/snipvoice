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
