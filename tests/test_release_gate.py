"""Offline contracts for the pre-restart release gate.

Only file contents are inspected: no service is restarted, no suite is run and
no credential, database or network resource is touched.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "deploy" / "pre-restart-gate.sh"
NODE_ENTRY = ROOT / "scripts" / "run_node_suites.sh"


class ReleaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = GATE.read_text(encoding="utf-8")
        self.node_entry = NODE_ENTRY.read_text(encoding="utf-8")

    def test_gate_runs_the_shared_node_suites(self) -> None:
        # The gate must not be Python-only: a restart decision has to cover the
        # same node suites that CI and the host release check execute.
        self.assertIn("scripts/run_node_suites.sh", self.gate)
        self.assertIn('"$ROOT/scripts/run_node_suites.sh"', self.gate)

    def test_gate_keeps_a_loud_opt_out_only(self) -> None:
        self.assertIn("--skip-node-suites", self.gate)
        # An opt-out has to announce itself, so a skipped suite is visible.
        self.assertIn("node suites skipped", self.gate)

    def test_gate_still_fails_closed_on_suite_failure(self) -> None:
        self.assertIn("set -euo pipefail", self.gate)

    def test_shared_entry_covers_both_node_suites_and_isolation(self) -> None:
        self.assertIn("tests/test_research_completion.mjs", self.node_entry)
        self.assertIn("tests/test_pi_cli_attachments.mjs", self.node_entry)
        # The attachment suite is only meaningful with an isolated namespace.
        self.assertIn("unshare --net", self.node_entry)
        # A missing environment must be reported, never silently skipped.
        self.assertIn("BLOCKED:", self.node_entry)
        self.assertIn("exit 2", self.node_entry)
        self.assertIn("PI_NATIVE_PACKAGE_DIR", self.node_entry)

    def test_shared_entry_syntax_and_mode_are_usable(self) -> None:
        body = self.node_entry
        self.assertTrue(body.startswith("#!/usr/bin/env bash"))
        self.assertTrue(re.search(r"(?m)^set -uo pipefail$", body))
        self.assertTrue(os.access(NODE_ENTRY, os.X_OK), "the shared entry point must be executable")


if __name__ == "__main__":
    unittest.main()
