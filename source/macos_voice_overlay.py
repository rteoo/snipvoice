"""Non-activating native voice-status panel for macOS.

Tk's native ``TKWindow`` activates its process the first time it is ordered,
even when the app uses accessory activation policy. A borderless ``NSPanel``
with the non-activating style is the supported Cocoa surface for an overlay
that must leave the dictation target frontmost.
"""

import ui_theme
from i18n import tr


_ACTION_TARGET = None


def _action_target_class():
    """Register one Objective-C target class, including across manager rebuilds."""
    global _ACTION_TARGET
    if _ACTION_TARGET is None:
        from Foundation import NSObject

        class SnipVoiceOverlayActions(NSObject):
            def open_(self, sender):
                if self.open_callback is not None:
                    self.open_callback()

            def dismiss_(self, sender):
                if self.dismiss_callback is not None:
                    self.dismiss_callback()

        _ACTION_TARGET = SnipVoiceOverlayActions
    return _ACTION_TARGET


def nonactivating_panel_style(appkit):
    """Return the two load-bearing style bits for the native overlay."""
    return (
        appkit.NSWindowStyleMaskBorderless
        | appkit.NSWindowStyleMaskNonactivatingPanel
    )


def order_panel_without_activation(appkit, panel):
    """Order the first Cocoa panel without letting Tk activate its process.

    Tk's shared ``NSApplication`` activates on the first window order even for
    a correctly styled non-activating ``NSPanel``. Briefly making the app
    ineligible for activation closes that framework-level gap; the original
    accessory policy is restored before this function returns.
    """
    application = appkit.NSApplication.sharedApplication()
    original_policy = application.activationPolicy()
    prohibited = appkit.NSApplicationActivationPolicyProhibited
    restore_policy = original_policy != prohibited
    if restore_policy and not application.setActivationPolicy_(prohibited):
        return False
    try:
        panel.orderFrontRegardless()
    finally:
        if restore_policy:
            application.setActivationPolicy_(original_policy)
    return True


class MacVoiceStatusPanel:
    """Nonactivating panel; open callbacks must enqueue to the Tk pump."""

    WIDTH = 220
    HEIGHT = 42
    BOTTOM_MARGIN = 72

    def __init__(self, on_open=None, on_dismiss=None):
        import AppKit

        self._appkit = AppKit
        style = nonactivating_panel_style(AppKit)
        frame = AppKit.NSMakeRect(0, 0, self.WIDTH, self.HEIGHT)
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        if panel is None:
            raise RuntimeError("macOS did not create the voice status panel")
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setLevel_(AppKit.NSFloatingWindowLevel)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setIgnoresMouseEvents_(False)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        content = AppKit.NSView.alloc().initWithFrame_(frame)
        content.setWantsLayer_(True)
        layer = content.layer()
        layer.setCornerRadius_(10.0)
        layer.setBackgroundColor_(
            AppKit.NSColor.windowBackgroundColor()
            .colorWithAlphaComponent_(0.96)
            .CGColor()
        )

        dot = AppKit.NSTextField.labelWithString_("●")
        dot.setFrame_(AppKit.NSMakeRect(14, 10, 20, 22))
        dot.setFont_(
            AppKit.NSFont.systemFontOfSize_weight_(14, AppKit.NSFontWeightSemibold)
        )
        content.addSubview_(dot)

        title = AppKit.NSTextField.labelWithString_("")
        title.setFrame_(AppKit.NSMakeRect(40, 10, 142, 22))
        title.setFont_(
            AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold)
        )
        title.setTextColor_(AppKit.NSColor.labelColor())
        content.addSubview_(title)

        target = _action_target_class().alloc().init()
        target.open_callback, target.dismiss_callback = on_open, on_dismiss
        self._action_target = target  # Cocoa target/action does not retain its target.
        for frame, action, label, tooltip in (
            (AppKit.NSMakeRect(0, 0, 184, self.HEIGHT), "open:", "", tr("Abrir")),
            (AppKit.NSMakeRect(184, 0, 36, self.HEIGHT), "dismiss:", "×", tr("Fechar")),
        ):
            button = AppKit.NSButton.alloc().initWithFrame_(frame)
            button.setBordered_(False)
            button.setTitle_(label)
            button.setToolTip_(tooltip)
            button.setTarget_(target)
            button.setAction_(action)
            content.addSubview_(button)

        panel.setContentView_(content)
        self.panel = panel
        self.dot = dot
        self.title = title

    def update(self, title, tone):
        self.title.setStringValue_(title)
        self.dot.setTextColor_(self._tone_color(tone))
        self._position()
        return order_panel_without_activation(self._appkit, self.panel)

    def hide(self):
        self.panel.orderOut_(None)

    def destroy(self):
        self._action_target.open_callback = None
        self._action_target.dismiss_callback = None
        self.panel.orderOut_(None)
        self.panel.close()

    def is_visible(self):
        return bool(self.panel.isVisible())

    def _tone_color(self, tone):
        """The status dot: Ink & Copper for live capture, system colors otherwise."""
        colors = self._appkit.NSColor
        if tone == "live":
            red, green, blue = ui_theme.rgb(ui_theme.theme().overlay_live)
            return colors.colorWithSRGBRed_green_blue_alpha_(red, green, blue, 1.0)
        if tone == "ok":
            return colors.systemGreenColor()
        if tone == "trouble":
            return colors.systemRedColor()
        if tone == "muted":
            return colors.systemGrayColor()
        return colors.controlAccentColor()

    def _position(self):
        screen = self._appkit.NSScreen.mainScreen()
        if screen is None:
            return
        visible = screen.visibleFrame()
        x = visible.origin.x + max(12, (visible.size.width - self.WIDTH) / 2)
        y = visible.origin.y + self.BOTTOM_MARGIN
        self.panel.setFrameOrigin_((x, y))
