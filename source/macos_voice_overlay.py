"""Non-activating native voice-status panel for macOS.

Tk's native ``TKWindow`` activates its process the first time it is ordered,
even when the app uses accessory activation policy. A borderless ``NSPanel``
with the non-activating style is the supported Cocoa surface for an overlay
that must leave the dictation target frontmost.
"""


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
    """Small click-through panel; call only from the Tk pump on macOS."""

    WIDTH = 220
    HEIGHT = 42
    BOTTOM_MARGIN = 72

    def __init__(self):
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
        panel.setIgnoresMouseEvents_(True)
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
        title.setFrame_(AppKit.NSMakeRect(40, 10, 166, 22))
        title.setFont_(
            AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold)
        )
        title.setTextColor_(AppKit.NSColor.labelColor())
        content.addSubview_(title)

        panel.setContentView_(content)
        self.panel = panel
        self.dot = dot
        self.title = title

    def update(self, title, accent_name):
        self.title.setStringValue_(title)
        self.dot.setTextColor_(self._accent_color(accent_name))
        self._position()
        return order_panel_without_activation(self._appkit, self.panel)

    def hide(self):
        self.panel.orderOut_(None)

    def destroy(self):
        self.panel.orderOut_(None)
        self.panel.close()

    def is_visible(self):
        return bool(self.panel.isVisible())

    def _accent_color(self, name):
        if name == "warning":
            return self._appkit.NSColor.systemOrangeColor()
        if name == "success":
            return self._appkit.NSColor.systemGreenColor()
        return self._appkit.NSColor.controlAccentColor()

    def _position(self):
        screen = self._appkit.NSScreen.mainScreen()
        if screen is None:
            return
        visible = screen.visibleFrame()
        x = visible.origin.x + max(12, (visible.size.width - self.WIDTH) / 2)
        y = visible.origin.y + self.BOTTOM_MARGIN
        self.panel.setFrameOrigin_((x, y))
