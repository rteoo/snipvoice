import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from macos_voice_overlay import MacVoiceStatusPanel, nonactivating_panel_style, order_panel_without_activation


class MacVoiceOverlayPolicyTests(unittest.TestCase):
    def test_clickable_controls_keep_panel_nonactivating_and_bind_both_actions(self):
        appkit = mock.Mock()
        appkit.NSWindowStyleMaskBorderless = 1
        appkit.NSWindowStyleMaskNonactivatingPanel = 128
        appkit.NSWindowCollectionBehaviorCanJoinAllSpaces = 2
        appkit.NSWindowCollectionBehaviorFullScreenAuxiliary = 4
        target = mock.Mock()
        opened, dismissed = mock.Mock(), mock.Mock()
        with mock.patch.dict(sys.modules, {"AppKit": appkit}), \
                mock.patch("macos_voice_overlay._action_target_class") as target_class:
            target_class.return_value.alloc.return_value.init.return_value = target
            overlay = MacVoiceStatusPanel(on_open=opened, on_dismiss=dismissed)
        panel = overlay.panel
        self.assertEqual(appkit.NSPanel.alloc.return_value
                         .initWithContentRect_styleMask_backing_defer_.call_args.args[1], 129)
        panel.setIgnoresMouseEvents_.assert_called_once_with(False)
        panel.setBecomesKeyOnlyIfNeeded_.assert_called_once_with(True)
        self.assertIs(target.open_callback, opened)
        self.assertIs(target.dismiss_callback, dismissed)
        button = appkit.NSButton.alloc.return_value.initWithFrame_.return_value
        self.assertEqual(button.setAction_.call_args_list, [mock.call("open:"), mock.call("dismiss:")])
        panel.makeKeyAndOrderFront_.assert_not_called()
        overlay.destroy()
        self.assertIsNone(target.open_callback)
        self.assertIsNone(target.dismiss_callback)

    def test_panel_is_borderless_and_nonactivating(self):
        appkit = types.SimpleNamespace(
            NSWindowStyleMaskBorderless=0x01,
            NSWindowStyleMaskNonactivatingPanel=0x80,
        )
        self.assertEqual(nonactivating_panel_style(appkit), 0x81)

    def test_order_temporarily_prohibits_activation_then_restores_policy(self):
        application = mock.Mock()
        application.activationPolicy.return_value = 1
        application.setActivationPolicy_.return_value = True
        appkit = types.SimpleNamespace(
            NSApplication=types.SimpleNamespace(
                sharedApplication=mock.Mock(return_value=application)
            ),
            NSApplicationActivationPolicyProhibited=0,
        )
        panel = mock.Mock()

        self.assertTrue(order_panel_without_activation(appkit, panel))

        self.assertEqual(
            application.setActivationPolicy_.call_args_list,
            [mock.call(0), mock.call(1)],
        )
        panel.orderFrontRegardless.assert_called_once_with()

    def test_failed_policy_change_refuses_to_order_panel(self):
        application = mock.Mock()
        application.activationPolicy.return_value = 1
        application.setActivationPolicy_.return_value = False
        appkit = types.SimpleNamespace(
            NSApplication=types.SimpleNamespace(
                sharedApplication=mock.Mock(return_value=application)
            ),
            NSApplicationActivationPolicyProhibited=0,
        )
        panel = mock.Mock()

        self.assertFalse(order_panel_without_activation(appkit, panel))
        panel.orderFrontRegardless.assert_not_called()


if __name__ == "__main__":
    unittest.main()
