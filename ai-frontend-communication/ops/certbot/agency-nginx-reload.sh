#!/usr/bin/env bash
set -Eeuo pipefail

readonly container=kakadudocs-nginx-1
readonly docker=/usr/bin/docker

"$docker" exec "$container" nginx -t
"$docker" exec "$container" nginx -s reload
