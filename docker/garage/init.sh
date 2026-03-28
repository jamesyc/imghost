#!/bin/sh
set -eu

CONFIG_FILE=/etc/garage.toml

STATUS_OUTPUT=$(/garage -c "$CONFIG_FILE" status)
if printf '%s\n' "$STATUS_OUTPUT" | grep -q "NO ROLE ASSIGNED"; then
    NODE_ID=$(/garage -c "$CONFIG_FILE" node id | awk 'NR==1 { split($1, parts, "@"); print parts[1] }')
    /garage -c "$CONFIG_FILE" layout assign -z "${GARAGE_ZONE:-dc1}" -c "${GARAGE_CAPACITY:-1G}" "$NODE_ID"
    /garage -c "$CONFIG_FILE" layout apply --version 1
fi

if ! /garage -c "$CONFIG_FILE" key info "${GARAGE_KEY_NAME:-imghost-app}" >/dev/null 2>&1; then
    /garage -c "$CONFIG_FILE" key import --yes -n "${GARAGE_KEY_NAME:-imghost-app}" "${S3_ACCESS_KEY_ID}" "${S3_SECRET_ACCESS_KEY}"
fi

if ! /garage -c "$CONFIG_FILE" bucket info "${S3_BUCKET}" >/dev/null 2>&1; then
    /garage -c "$CONFIG_FILE" bucket create "${S3_BUCKET}"
fi

/garage -c "$CONFIG_FILE" bucket allow --read --write --owner "${S3_BUCKET}" --key "${GARAGE_KEY_NAME:-imghost-app}"
