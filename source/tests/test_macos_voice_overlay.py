import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from macos_voice_overlay import nonactivating_panel_style, order_panel_without_activation


class MacVoiceOverlayPolicyTests(unittest.TestCase):
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
