#!/usr/bin/env bash
# setup-local-codesign.sh — One-time setup for persistent local code-signing.
#
# Why: macOS TCC keys ad-hoc-signed apps by cdhash, which changes on every
# rebuild. That means re-granting Accessibility, Input Monitoring, Microphone,
# and Screen Recording every single time you iterate on the client. With a
# stable self-signed cert in the System trust store, TCC matches on the cert
# identity instead and grants persist across rebuilds.
#
# What this does:
#   1. Generates a self-signed code-signing cert (if not already present in
#      the login keychain).
#   2. Imports it with codesign access (-T /usr/bin/codesign).
#   3. Adds it to the System keychain as a trusted code-signing root
#      (this is the step that requires sudo).
#
# After running this, scripts/build-client-local.sh picks up the identity
# automatically and TCC grants survive rebuilds — you re-grant ONCE.
#
# Idempotent: safe to re-run.

set -euo pipefail

CERT_NAME="WisprAlt Local Dev"
LOGIN_KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
SYSTEM_KEYCHAIN="/Library/Keychains/System.keychain"
TMP_DIR="$(mktemp -d -t wispralt-codesign-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ── Step 1: Check if identity already exists ──────────────────────────────────
echo "Step 1/4: Checking for existing identity '$CERT_NAME'..."
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$CERT_NAME"; then
    echo "  ✓ already in login keychain — reusing"
    SKIP_CREATE=1
else
    echo "  not found — will generate"
    SKIP_CREATE=0
fi

# ── Step 2: Generate cert if needed ───────────────────────────────────────────
if [[ "$SKIP_CREATE" == "0" ]]; then
    echo "Step 2/4: Generating self-signed code-signing cert..."

    cat > "$TMP_DIR/cert.cnf" <<EOF
[ req ]
distinguished_name = req_dn
prompt             = no
[ req_dn ]
CN = $CERT_NAME
O  = WisprAlt Dev
[ v3_codesign ]
basicConstraints     = critical, CA:false
keyUsage             = critical, digitalSignature
extendedKeyUsage     = critical, codeSigning
subjectKeyIdentifier = hash
EOF

    openssl req -new -x509 \
        -newkey rsa:2048 -nodes \
        -keyout "$TMP_DIR/cert.key" \
        -out    "$TMP_DIR/cert.pem" \
        -days   3650 \
        -config "$TMP_DIR/cert.cnf" \
        -extensions v3_codesign \
        2>/dev/null

    PASSPHRASE="$(openssl rand -hex 16)"
    openssl pkcs12 -export -legacy \
        -inkey  "$TMP_DIR/cert.key" \
        -in     "$TMP_DIR/cert.pem" \
        -out    "$TMP_DIR/cert.p12" \
        -name   "$CERT_NAME" \
        -passout pass:"$PASSPHRASE"

    echo "  Importing into login keychain (codesign + security may use it)..."
    security import "$TMP_DIR/cert.p12" \
        -k "$LOGIN_KEYCHAIN" \
        -P "$PASSPHRASE" \
        -T /usr/bin/codesign \
        -T /usr/bin/security

    # Allow codesign to use the private key without prompting on every build.
    security set-key-partition-list \
        -S apple-tool:,apple:,codesign: \
        -s -k "" \
        "$LOGIN_KEYCHAIN" 2>/dev/null || true

    echo "  ✓ identity imported"
else
    echo "Step 2/4: Skipped (identity already present)"
fi

# ── Step 3: Export the cert (.pem) for trust step ─────────────────────────────
echo "Step 3/4: Exporting cert for System trust import..."
security find-certificate -c "$CERT_NAME" -p login.keychain > "$TMP_DIR/cert-pubkey.pem"
if [[ ! -s "$TMP_DIR/cert-pubkey.pem" ]]; then
    echo "ERROR: could not export public cert from login keychain" >&2
    exit 1
fi
echo "  ✓ exported"

# ── Step 4: Add to System trust store as code-signing root ───────────────────
echo "Step 4/4: Adding cert to System keychain as trusted code-signing root..."
echo "          This requires your password (sudo)."
sudo security add-trusted-cert \
    -d \
    -r trustRoot \
    -p codeSign \
    -k "$SYSTEM_KEYCHAIN" \
    "$TMP_DIR/cert-pubkey.pem"
echo "  ✓ trusted"

echo ""
echo "──────────────────────────────────────────────"
echo "Setup complete."
echo ""
echo "From now on, scripts/build-client-local.sh will sign with '$CERT_NAME'."
echo "After your NEXT rebuild + reinstall, you'll re-grant the 4 permissions"
echo "ONE LAST TIME. After that, future rebuilds preserve all TCC grants."
echo ""
echo "To revoke later:"
echo "  sudo security remove-trusted-cert -d $TMP_DIR/cert-pubkey.pem  (no — temp gone)"
echo "  sudo security delete-certificate -c \"$CERT_NAME\" $SYSTEM_KEYCHAIN"
echo "  security delete-identity -c \"$CERT_NAME\" $LOGIN_KEYCHAIN"
