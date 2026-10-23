#!/bin/sh
# Rebuild the container's CA bundle without the trust anchors distrusted after
# the eMudhra intermediate CA key compromise (CVE-2026-113470).
#
# Vector's vendored OpenSSL resolves trust through SSL_CERT_FILE/SSL_CERT_DIR,
# so pointing those at the rebuilt bundle moves every sink off the image's
# stock trust store at once -- no per-sink tls.ca_file, and no sink added later
# can miss it.
#
# The upstream Vector image is debian:trixie-slim with ca-certificates installed
# by apt, so /etc/ssl/certs holds one PEM per anchor and the rebuild is a
# filtered concatenation. Once upstream ships an image whose ca-certificates
# already drops these roots, the named files are simply absent and this becomes
# a no-op that can be deleted.
#
# Fails closed: if the bundle cannot be rebuilt the container exits rather than
# starting with a trust store nobody has checked.
set -eu

CERT_DIR="${CWA_CA_CERT_DIR:-/etc/ssl/certs}"
OUT_DIR="${CWA_CA_OUT_DIR:-/etc/crusoe-ca-trust}"
DENYLIST="${CWA_CA_DENYLIST:-/etc/crusoe-ca/distrusted-roots.txt}"
OUT="$OUT_DIR/ca-certificates.crt"

log() { echo "crusoe-ca-trust: $*"; }

[ -d "$CERT_DIR" ] || { log "no certificate directory at $CERT_DIR" >&2; exit 1; }

# OpenSSL reads SSL_CERT_DIR in addition to SSL_CERT_FILE, so it gets an empty
# directory of its own. Leaving it pointed at the image's hashed directory
# would keep the distrusted roots reachable.
mkdir -p "$OUT_DIR/certs.d"

# One filename per line; blank lines and # comments ignored.
distrusted=""
if [ -r "$DENYLIST" ]; then
  distrusted=$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$DENYLIST" | grep -v '^$' || true)
fi

# Report on each name rather than excluding it blindly: a root that has been
# renamed upstream would otherwise be dropped from the denylist silently and
# stay trusted.
set --
for name in $distrusted; do
  if [ -e "$CERT_DIR/$name" ]; then
    log "distrusting $name"
  else
    log "note: $name is not in $CERT_DIR -- nothing to distrust"
  fi
  set -- "$@" ! -name "$name"
done

find "$CERT_DIR" -maxdepth 1 -name '*.pem' "$@" -exec cat {} + > "$OUT"

kept=$(grep -c '^-----BEGIN CERTIFICATE-----$' "$OUT" || true)
if [ "$kept" -eq 0 ]; then
  log "refusing to start: rebuilt bundle has no trust anchors" >&2
  exit 1
fi

log "wrote $OUT ($kept anchors)"
