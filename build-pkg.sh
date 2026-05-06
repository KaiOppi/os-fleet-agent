#!/bin/sh
# Build os-fleet-agent .pkg on the OPNsense Dev VM.
# Run this script ON THE OPNsense (FreeBSD), not on your dev workstation.
set -eu

PLUGIN_DIR="${PLUGIN_DIR:-/root/os-fleet-agent-build}"
OUT_DIR="${OUT_DIR:-/root}"
VERSION=$(grep '"version"' "$PLUGIN_DIR/plus.json" | head -1 | sed 's/.*: "\(.*\)",/\1/')

echo "==> Building os-fleet-agent v$VERSION from $PLUGIN_DIR"

# Build a flat staging tree
STAGE=$(mktemp -d)
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

mkdir -p "$STAGE/usr/local"
cp -a "$PLUGIN_DIR/src/opnsense" "$STAGE/usr/local/"
chmod +x "$STAGE/usr/local/opnsense/scripts/OPNsense/Fleet/agent.py"

# pkg-plist — paths relative to prefix (/usr/local)
PLIST="$STAGE/pkg-plist"
( cd "$STAGE/usr/local" && find . -type f | sed 's|^\./||' | sort ) > "$PLIST"

# Manifest
cat > "$STAGE/+MANIFEST" <<EOF
{
  "name": "os-fleet-agent",
  "version": "$VERSION",
  "origin": "opnsense/os-fleet-agent",
  "comment": "os-fleet push-mode agent",
  "desc": "$(cat "$PLUGIN_DIR/pkg-descr" | tr '\n' ' ')",
  "maintainer": "info@it-service-nf.de",
  "www": "https://github.com/KaiOppi/os-fleet-agent",
  "abi": "FreeBSD:14:amd64",
  "arch": "FreeBSD:14:amd64",
  "prefix": "/usr/local",
  "categories": ["sysutils", "net-mgmt"],
  "licenselogic": "single",
  "licenses": ["BSD2CLAUSE"],
  "deps": {},
  "annotations": {
    "product_id": "os-fleet-agent",
    "product_name": "os-fleet-agent",
    "product_tier": "3",
    "product_version": "$VERSION",
    "FreeBSD_version": "1402000",
    "product_abi": "26.1"
  }
}
EOF

cp "$PLUGIN_DIR/pkg-message" "$STAGE/+DISPLAY" 2>/dev/null || true

OUT="$OUT_DIR/os-fleet-agent-${VERSION}.pkg"
( cd "$STAGE" && pkg create -m . -r . -p pkg-plist -o "$OUT_DIR" )

# pkg create writes by manifest name; rename predictably
if [ -f "$OUT_DIR/os-fleet-agent-${VERSION}.pkg" ]; then
    echo "==> Built $OUT_DIR/os-fleet-agent-${VERSION}.pkg"
elif [ -f "$OUT_DIR/os-fleet-agent.pkg" ]; then
    mv "$OUT_DIR/os-fleet-agent.pkg" "$OUT"
    echo "==> Built $OUT"
else
    echo "==> pkg create finished but output not found in $OUT_DIR"
    ls -la "$OUT_DIR"/*.pkg 2>/dev/null || true
fi
