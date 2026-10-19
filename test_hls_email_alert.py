import unittest
from unittest.mock import Mock

import hls_email_alert as alert


class EmailAlertTests(unittest.TestCase):
    def setUp(self):
        self.config = {"sender": alert.DEFAULT_SENDER, "recipient": alert.DEFAULT_RECIPIENT}
        self.send = Mock()

    def check(self, previous=None, **status):
        return alert.check_once(self.config, previous or {}, status, sender=self.send)

    def test_healthy_service_does_not_send(self):
        state, outcome = self.check(ActiveState="active", SubState="running")
        self.assertEqual((state, outcome), ({}, "healthy"))
        self.send.assert_not_called()

    def test_failure_sends_once_and_deduplicates(self):
        status = dict(ActiveState="failed", SubState="failed", Result="exit-code",
                      ExecMainStatus="1", InvocationID="one")
        state, outcome = self.check(**status)
        self.assertEqual(outcome, "alert-sent")
        self.assertIn("alert_key", state)
        self.assertEqual(self.send.call_count, 1)
        same_state, outcome = self.check(state, **status)
        self.assertEqual((same_state, outcome), (state, "already-alerted"))
        self.assertEqual(self.send.call_count, 1)

    def test_auto_restart_does_not_send_and_preserves_failure_state(self):
        state = {"alert_key": "earlier-failure"}
        updated, outcome = self.check(state, ActiveState="activating", SubState="auto-restart")
        self.assertEqual((updated, outcome), (state, "service-auto-restarting"))
        self.send.assert_not_called()

    def test_recovery_sends_and_clears_failure_state(self):
        updated, outcome = self.check({"alert_key": "earlier-failure"},
                                      ActiveState="active", SubState="running")
        self.assertEqual((updated, outcome), ({}, "recovered"))
        self.send.assert_called_once()

    def test_smtp_error_preserves_state_for_next_check(self):
        self.send.side_effect = ConnectionError("SMTP unavailable")
        with self.assertRaises(ConnectionError):
            self.check(ActiveState="failed", SubState="failed", Result="exit-code",
                       ExecMainStatus="1", InvocationID="one")


if __name__ == "__main__":
    unittest.main()
