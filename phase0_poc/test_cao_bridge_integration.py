"""Opt-in integration tests; never run as unit tests by default.

Set PHASE0_REAL_CAO=1 and CAO_SESSION_ID/CAO_TERMINAL_ID to a real, existing
CAO terminal to execute these tests.  Missing live prerequisites remain a skip,
not a green fake-provider result.
"""

import os
import unittest

try:
    from .cao_bridge import CaoRuntimeAgent
except ImportError:  # unittest discover -s phase0_poc imports this as a top-level module
    from cao_bridge import CaoRuntimeAgent


@unittest.skipUnless(
    os.environ.get("PHASE0_REAL_CAO") == "1"
    and os.environ.get("CAO_SESSION_ID")
    and os.environ.get("CAO_TERMINAL_ID"),
    "requires an explicitly selected real CAO terminal",
)
class RealCaoBridgeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = CaoRuntimeAgent(
            os.environ.get("CAO_BASE_URL", "http://127.0.0.1:9889"),
            os.environ["CAO_TERMINAL_ID"],
            os.environ["CAO_SESSION_ID"],
        )

    def test_real_terminal_status_and_output_are_observed(self) -> None:
        snapshot = self.agent.snapshot()
        self.assertNotEqual(snapshot.provider, "UNKNOWN")
        self.assertIn(snapshot.status, {"HEALTHY", "IDLE", "WORKING", "WAITING"})
        self.assertIn(snapshot.raw_status, {"idle", "processing", "working", "waiting_user_answer", "completed"})
        self.assertIsInstance(snapshot.output, str)


if __name__ == "__main__":
    unittest.main()
