"""Offline contracts for the pre-restart release gate.

Only file contents are inspected: no service is restarted, no suite is run and
no credential, database or network resource is touched.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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


class GateBranchBehaviourTests(unittest.TestCase):
    """Run the real script in a throwaway tree so skip paths are observable.

    PYTHON is pointed at /bin/true: the gate's own control flow and final
    message are under test here, not the Python suites (which have their own
    coverage elsewhere). No production path, database or credential is read.
    """

    def setUp(self) -> None:
        self.bash = shutil.which("bash")
        self.noop = shutil.which("true")
        if self.bash is None or self.noop is None:
            self.skipTest("bash and true are required")
        self.temporary = tempfile.TemporaryDirectory(prefix="gate-behaviour-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "deploy").mkdir()
        (self.root / "scripts").mkdir()
        shutil.copy(GATE, self.root / "deploy" / "pre-restart-gate.sh")
        stub = self.root / "scripts" / "run_node_suites.sh"
        stub.write_text("#!/bin/sh\nexit \"${NODE_SUITE_EXIT:-0}\"\n", encoding="utf-8")
        stub.chmod(0o755)
        (self.root / ".env").write_text("SYNTHETIC=yes\n", encoding="utf-8")

    def run_gate(self, *args: str, node_exit: str = "0") -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": "/usr/bin:/bin",
            "PYTHON": self.noop,
            "NODE_SUITE_EXIT": node_exit,
        }
        return subprocess.run(
            [self.bash, "--noprofile", "--norc",
             str(self.root / "deploy" / "pre-restart-gate.sh"), *args],
            cwd=self.root, env=environment, text=True, capture_output=True,
            timeout=30, check=False,
        )

    def test_complete_run_claims_restart_safety(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("safe to restart", result.stdout)
        self.assertNotIn("PARTIAL", result.stdout)

    def test_skipping_node_suites_never_claims_restart_safety(self) -> None:
        result = self.run_gate("--skip-node-suites")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("safe to restart", result.stdout)
        self.assertIn("PARTIAL", result.stdout)
        self.assertIn("不构成生产发布验收", result.stdout)
        self.assertIn("node suites", result.stdout)

    def test_skipping_self_test_never_claims_restart_safety(self) -> None:
        result = self.run_gate("--skip-self-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("safe to restart", result.stdout)
        self.assertIn("PARTIAL", result.stdout)

    def test_missing_env_self_skip_also_reports_partial(self) -> None:
        (self.root / ".env").unlink()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("safe to restart", result.stdout)
        self.assertIn("PARTIAL", result.stdout)
        self.assertIn("no .env", result.stdout)

    def test_failing_node_suite_aborts_before_any_restart_claim(self) -> None:
        result = self.run_gate(node_exit="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("safe to restart", result.stdout)
        self.assertNotIn("PARTIAL", result.stdout)


if __name__ == "__main__":
    unittest.main()
