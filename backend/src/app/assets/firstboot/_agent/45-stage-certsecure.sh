# Stage the product payload out of the firstboot files directory (product
# templates only).
#
# Staging, deliberately not installing. Firstboot runs *before* the agent
# exists, so anything that fails here fails invisibly — the only symptom is a
# phone-home that never comes, which is indistinguishable from a dozen other
# faults. A copy and an extract are the two things that cannot meaningfully fail
# on a disc that mounted; the 595-second vendor install runs afterwards as a
# sequence step with progress, retries and a visible error.
#
# The runner deletes $FIRSTBOOT_FILES_DIR after the run, so the tarball has to
# be moved out of it before then or the payload is simply gone.

set -euo pipefail

if [ -z "${FIRSTBOOT_FILES_DIR:-}" ]; then
    echo 'FIRSTBOOT_FILES_DIR is not set — this base image predates the v2 firstboot runner; rebuild the golden image before deploying a product template.' >&2
    exit 1
fi

payload="$FIRSTBOOT_FILES_DIR/__PAYLOAD_NAME__"
expected_sha256='__PAYLOAD_SHA256__'
install_dir='__INSTALL_DIR__'

if [ ! -f "$payload" ]; then
    echo "Product payload $payload is missing from the firstboot disc." >&2
    exit 1
fi

# Verified before the extract, not after: the disc is an ISO built on the worker
# and read over a virtual optical device, and a truncated read would otherwise
# surface as a tar error deep in the tree rather than as a bad payload.
actual_sha256="$(sha256sum "$payload" | cut -d' ' -f1)"
if [ "$actual_sha256" != "$expected_sha256" ]; then
    echo "Product payload digest mismatch: expected $expected_sha256, got $actual_sha256." >&2
    exit 1
fi

# The tarball has a single top-level directory; --strip-components=1 lands its
# contents directly in $install_dir, which is the fixed path both the agent's
# certsecure.* commands and the file.* relay allowlist are written against.
mkdir -p "$install_dir"
tar -xzf "$payload" -C "$install_dir" --strip-components=1
rm -f "$payload"

echo "product payload staged to $install_dir"
