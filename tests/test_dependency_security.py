"""Offline guards for the pinned HTTP dependency security update.

These are unittest tests and are also collected by pytest. Missing packages or
an unsafe installed version fail, rather than skipping the security check. No
application settings, .env files, live endpoints or model clients are used.
"""

from contextlib import contextmanager
import importlib
from importlib import metadata
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_HTTP_VERSIONS = {"httpx2": "2.12.0", "httpcore2": "2.12.0"}


@contextmanager
def no_network():
    """Catch accidental DNS/socket use even if a transport is misconfigured."""
    with (
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("unexpected DNS")),
        patch.object(socket, "create_connection", side_effect=AssertionError("unexpected network")),
        patch.object(socket.socket, "connect", side_effect=AssertionError("unexpected network")),
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("unexpected network")),
    ):
        yield


class DependencySecurityTests(unittest.TestCase):
    def test_both_manifests_have_one_unconditional_exact_safe_pin(self) -> None:
        for filename in ("requirements.lock", "requirements-frozen.txt"):
            requirements = []
            for line in (PROJECT_ROOT / filename).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    requirements.append(Requirement(line))
            for package, safe_version in SAFE_HTTP_VERSIONS.items():
                with self.subTest(manifest=filename, package=package):
                    matches = [requirement for requirement in requirements
                               if canonicalize_name(requirement.name) == package]
                    self.assertEqual(len(matches), 1, "require exactly one explicit security pin")
                    requirement = matches[0]
                    self.assertEqual(str(requirement.specifier), f"=={safe_version}")
                    self.assertIsNone(requirement.marker, "security pins must not be conditional")
                    self.assertIsNone(requirement.url, "security pins must use a versioned release")
                    self.assertFalse(requirement.extras)

    def test_installed_http_dependencies_meet_safe_release_floor(self) -> None:
        self.assertEqual(Version(metadata.version("httpx2")),
                         Version(metadata.version("httpcore2")),
                         "httpx2 and httpcore2 must remain in lockstep")
        for package, safe_version in SAFE_HTTP_VERSIONS.items():
            with self.subTest(package=package):
                installed = Version(metadata.version(package))
                self.assertGreaterEqual(installed, Version(safe_version))
                self.assertFalse(installed.is_prerelease, "require a released security fix")
                self.assertFalse(installed.is_devrelease, "development builds are not verified fixes")

    def test_httpx2_sync_mock_transport_remains_compatible(self) -> None:
        with no_network():
            httpx2 = importlib.import_module("httpx2")
            seen = []

            def handle(request):
                seen.append((request.method, request.url.host, request.url.path))
                return httpx2.Response(200, json={"ok": True, "transport": "offline"})

            with httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False) as client:
                response = client.get("https://dependency-test.invalid/health")
                response.raise_for_status()
                self.assertEqual(response.json(), {"ok": True, "transport": "offline"})
            self.assertEqual(seen, [("GET", "dependency-test.invalid", "/health")])

    def test_genai_prices_import_and_httpx2_requirement_remain_compatible(self) -> None:
        with no_network():
            package = importlib.import_module("genai_prices")
            self.assertEqual(package.__name__, "genai_prices")
            requirements = [Requirement(value) for value in metadata.requires("genai-prices") or []]
            active_httpx2 = [requirement for requirement in requirements
                            if canonicalize_name(requirement.name) == "httpx2"
                            and (requirement.marker is None or requirement.marker.evaluate({"extra": ""}))]
            self.assertTrue(active_httpx2, "genai-prices should declare its active httpx2 dependency")
            installed = Version(metadata.version("httpx2"))
            for requirement in active_httpx2:
                self.assertIsNone(requirement.url)
                self.assertTrue(requirement.specifier.contains(installed, prereleases=False),
                                "installed httpx2 must satisfy genai-prices dependency metadata")


class AsyncDependencyCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_httpx2_async_mock_transport_remains_compatible(self) -> None:
        with no_network():
            httpx2 = importlib.import_module("httpx2")
            seen = []

            async def handle(request):
                seen.append((request.method, request.url.host, request.url.path))
                return httpx2.Response(200, json={"ok": True, "transport": "offline-async"})

            async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle), trust_env=False) as client:
                response = await client.get("https://dependency-test.invalid/health")
                response.raise_for_status()
                self.assertEqual(response.json(), {"ok": True, "transport": "offline-async"})
            self.assertEqual(seen, [("GET", "dependency-test.invalid", "/health")])


if __name__ == "__main__":
    unittest.main()
