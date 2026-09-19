#!/usr/bin/env bash
# Node suites that must accompany the Python suite in any release check.
#
# Covers the pi research-relay unit tests and the installed-pi CLI attachment
# suite. The attachment suite is only meaningful inside an isolated network
# namespace, so it runs under `unshare --net`; without that isolation it fails
# on purpose instead of reporting a false pass.
#
# Exit codes: 0 pass, 1 suite failed, 2 environment cannot run the suites.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_BIN="$(command -v node || true)"
if [[ -z "${NODE_BIN}" ]]; then
    echo "BLOCKED: node is not installed" >&2
    exit 2
fi

# The production install path is /usr/lib/node_modules; a CI or sandbox run can
# point PI_NATIVE_PACKAGE_DIR (or a different npm prefix) at its own copy.
PI_DIR="${PI_NATIVE_PACKAGE_DIR:-}"
if [[ -z "${PI_DIR}" ]]; then
    PI_ROOT="$(npm root -g 2>/dev/null || true)"
    [[ -n "${PI_ROOT}" ]] || PI_ROOT=/usr/lib/node_modules
    PI_DIR="${PI_ROOT}/@earendil-works/pi-coding-agent"
fi
if [[ ! -d "${PI_DIR}" ]]; then
    echo "BLOCKED: installed pi package not found at ${PI_DIR}" >&2
    exit 2
fi
if ! command -v unshare >/dev/null 2>&1; then
    echo "BLOCKED: unshare is required for the network-isolated suite" >&2
    exit 2
fi

published_version="$("${NODE_BIN}" -e \
    "process.stdout.write(require(process.argv[1]).version)" \
    "${PI_DIR}/package.json" 2>/dev/null || true)"
echo "pi package: ${PI_DIR} version=${published_version:-unknown}"

cd "${REPO_ROOT}" || exit 2
"${NODE_BIN}" --test tests/test_research_completion.mjs || exit 1

# Empty environment and an explicit package path: the suite must not depend on
# the caller's PATH, credentials or an unisolated network namespace.
unshare --net -- env -i PATH=/usr/bin:/bin HOME=/tmp \
    PI_NATIVE_PACKAGE_DIR="${PI_DIR}" \
    "${NODE_BIN}" --test "${REPO_ROOT}/tests/test_pi_cli_attachments.mjs" || exit 1

echo "node suites: ok"
exit 0
