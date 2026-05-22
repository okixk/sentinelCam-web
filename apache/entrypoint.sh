#!/bin/sh
# Generate a long-lived self-signed certificate on first start so the stack
# can serve HTTPS out of the box. Replace /etc/apache/certs/server.crt and
# server.key with files from a real CA (Let's Encrypt etc.) at any time and
# the existing files will be left alone.
set -eu

CERT_DIR=/etc/apache/certs
CERT="${CERT_DIR}/server.crt"
KEY="${CERT_DIR}/server.key"
HOSTNAME="${SC_PUBLIC_HOSTNAME:-sentinelcam.local}"
ALT_HOSTNAME="${SC_ALT_HOSTNAME:-studygames.local}"
DEFINE_FILE=/usr/local/apache2/conf/extra/sc-define.conf

mkdir -p "${CERT_DIR}"

if [ ! -s "${CERT}" ] || [ ! -s "${KEY}" ]; then
    echo "[apache] No certificate at ${CERT}; generating self-signed for ${HOSTNAME}, ${ALT_HOSTNAME}" >&2
    openssl req -x509 -nodes \
        -newkey rsa:2048 \
        -keyout "${KEY}" \
        -out "${CERT}" \
        -days 3650 \
        -subj "/CN=${HOSTNAME}" \
        -addext "subjectAltName=DNS:${HOSTNAME},DNS:${ALT_HOSTNAME},DNS:localhost,IP:127.0.0.1" \
        >/dev/null 2>&1
    chmod 600 "${KEY}"
fi

# Apache only expands ${VAR} in its config when VAR was set via a Define
# directive (env vars are not interpolated). Write a small define file the
# main config includes so the vhosts can reference ${SC_PUBLIC_HOSTNAME} and
# ${SC_ALT_HOSTNAME}.
cat > "${DEFINE_FILE}" <<EOF
Define SC_PUBLIC_HOSTNAME "${HOSTNAME}"
Define SC_ALT_HOSTNAME "${ALT_HOSTNAME}"
EOF

exec "$@"
