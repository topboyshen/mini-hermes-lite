"""Offline guards for pinned HTTP and JWT dependency security updates.

These are unittest tests and are also collected by pytest. Missing packages or
an unsafe installed version fail, rather than skipping the security check. No
application settings, .env files, live endpoints or model clients are used.
"""

from contextlib import contextmanager
import importlib
from importlib import metadata
from io import BytesIO
from pathlib import Path
import socket
import ssl
import unittest
from unittest.mock import patch
import zlib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAFE_DEPENDENCY_VERSIONS = {
    "httpx2": "2.12.0", "httpcore2": "2.12.0",
    "pyjwt": "2.15.0", "urllib3": "2.8.0",
}


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
            for package, safe_version in SAFE_DEPENDENCY_VERSIONS.items():
                with self.subTest(manifest=filename, package=package):
                    matches = [requirement for requirement in requirements
                               if canonicalize_name(requirement.name) == package]
                    self.assertEqual(len(matches), 1, "require exactly one explicit security pin")
                    requirement = matches[0]
                    self.assertEqual(str(requirement.specifier), f"=={safe_version}")
                    self.assertIsNone(requirement.marker, "security pins must not be conditional")
                    self.assertIsNone(requirement.url, "security pins must use a versioned release")
                    self.assertFalse(requirement.extras)

    def test_installed_dependencies_meet_safe_release_floor(self) -> None:
        self.assertEqual(Version(metadata.version("httpx2")),
                         Version(metadata.version("httpcore2")),
                         "httpx2 and httpcore2 must remain in lockstep")
        for package, safe_version in SAFE_DEPENDENCY_VERSIONS.items():
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


class JwtDependencySecurityTests(unittest.TestCase):
    def test_signed_roundtrip_and_invalid_signature(self) -> None:
        with no_network():
            import jwt
            signing_material = b"offline-test-signing-material-32-bytes-minimum"
            token = jwt.encode({"sub": "offline-user"}, signing_material, algorithm="HS256")
            self.assertEqual(jwt.decode(token, signing_material, algorithms=["HS256"]),
                             {"sub": "offline-user"})
            with self.assertRaises(jwt.InvalidSignatureError):
                jwt.decode(token, b"different-offline-signing-material-at-least-32", algorithms=["HS256"])

    def test_unverified_decode_does_not_mutate_options_or_disable_later_expiry(self) -> None:
        # GHSA-gvp8-978c-rx2q: reuse must not carry disabled claim checks forward.
        with no_network():
            import jwt
            signing_material = b"offline-test-signing-material-32-bytes-minimum"
            token = jwt.encode({"exp": 1}, signing_material, algorithm="HS256")
            options = {"verify_signature": False}
            self.assertEqual(jwt.decode(token, options=options), {"exp": 1})
            self.assertEqual(options, {"verify_signature": False})
            options["verify_signature"] = True
            with self.assertRaises(jwt.ExpiredSignatureError):
                jwt.decode(token, signing_material, algorithms=["HS256"], options=options)
            self.assertEqual(options, {"verify_signature": True})

    def test_empty_hmac_jwk_is_rejected(self) -> None:
        with no_network():
            import jwt
            with self.assertRaises(jwt.InvalidKeyError):
                jwt.PyJWK.from_dict({"kty": "oct", "k": "", "alg": "HS256"})

    def test_whitespace_modified_public_pem_is_not_an_hmac_secret(self) -> None:
        # Generated ephemeral public keys only; nothing is written to disk.
        with no_network():
            import jwt
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            public = private.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            # GHSA-ffc3-869f-jxw9: these are still parseable RSA public keys,
            # but PyJWT 2.13.0 accepted each as an HMAC signing secret.
            variants = {
                "end_tab": public.replace(b"-----END", b"\t-----END"),
                "cr_only": public.replace(b"\n", b"\r"),
                "folded": b" ".join(public.splitlines()),
            }
            for name, variant in variants.items():
                with self.subTest(variant=name):
                    parsed = serialization.load_pem_public_key(variant)
                    self.assertEqual(parsed.public_numbers(), private.public_key().public_numbers())
                    with self.assertRaises(jwt.InvalidKeyError):
                        jwt.encode({"sub": "offline-user"}, variant, algorithm="HS256")

    def test_deep_unverified_json_is_rejected_without_recursion_error(self) -> None:
        with no_network():
            import jwt
            from jwt.utils import base64url_encode
            nested = b'{"value":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
            header = b'{"alg":"HS256","typ":"JWT"}'
            for first, second in ((nested, b"{}"), (header, nested)):
                with self.subTest(nested_part="header" if first is nested else "payload"):
                    token = b".".join((base64url_encode(first), base64url_encode(second), b""))
                    with self.assertRaises(jwt.DecodeError):
                        jwt.decode(token, options={"verify_signature": False})


class Urllib3DependencyCompatibilityTests(unittest.TestCase):
    def test_requests_decodes_urllib3_deflate_stream_without_network(self) -> None:
        with no_network():
            import requests
            import urllib3
            body = b'{"ok": true, "source": "offline"}'
            raw = urllib3.HTTPResponse(
                body=BytesIO(zlib.compress(body)), status=200,
                headers={"Content-Encoding": "deflate", "Content-Type": "application/json"},
                preload_content=False,
            )
            prepared = requests.Request("GET", "https://dependency-test.invalid/health").prepare()
            adapter = requests.adapters.HTTPAdapter()
            try:
                response = adapter.build_response(prepared, raw)
                self.assertEqual(b"".join(response.iter_content(chunk_size=3)), body)
                response.close()
            finally:
                adapter.close()

    def test_https_proxy_and_destination_tls_contexts_remain_separate(self) -> None:
        with no_network():
            import urllib3
            proxy_context = ssl.create_default_context()
            target_context = ssl.create_default_context()
            manager = urllib3.ProxyManager(
                "https://proxy.dependency-test.invalid:8443",
                proxy_ssl_context=proxy_context, ssl_context=target_context,
                proxy_assert_hostname="proxy.dependency-test.invalid",
            )
            try:
                pool = manager.connection_from_url("https://dependency-test.invalid")
                self.assertIs(pool.conn_kw["ssl_context"], target_context)
                self.assertIs(pool.conn_kw["proxy_config"].ssl_context, proxy_context)
                self.assertEqual(proxy_context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(proxy_context.check_hostname)
            finally:
                manager.clear()


class AsyncDependencyCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_client_assertions_remain_valid_with_pyjwt(self) -> None:
        with no_network():
            import jwt
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from mcp.client.auth.extensions.client_credentials import (
                JWTParameters, SignedJWTParameters,
            )
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
            provider = SignedJWTParameters(
                issuer="offline-client", subject="offline-client", signing_key=pem,
            ).create_assertion_provider()
            asynchronous = await provider("https://auth.invalid")
            synchronous = JWTParameters(
                issuer="offline-client", subject="offline-client", jwt_signing_key=pem,
            ).to_assertion(with_audience_fallback="https://auth.invalid")
            for assertion in (asynchronous, synchronous):
                claims = jwt.decode(
                    assertion, key.public_key(), algorithms=["RS256"],
                    audience="https://auth.invalid", issuer="offline-client",
                    options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
                )
                self.assertEqual(claims["sub"], "offline-client")
                self.assertEqual(claims["exp"] - claims["iat"], 300)

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
