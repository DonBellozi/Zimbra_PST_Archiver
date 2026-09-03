#!/bin/sh
set -eu

# Docker named volumes are initially owned by root. Fix only application
# directories, then run the service without root privileges.
mkdir -p /data/db /data/work /data/pst /data/nas
chown -R archiver:archiver /data/db /data/work /data/pst

exec gosu archiver "$@"
