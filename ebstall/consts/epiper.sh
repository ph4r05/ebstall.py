#!/usr/bin/env bash
#
# Enigma PIP command invoker
#

if [ -z "$1" ]; then
    echo "Usage: $0 pip-installed-script args"
    exit 1
fi

SCRIPT=$1
shift

if [ -f "/usr/local/bin/${SCRIPT}" ]; then
    /usr/local/bin/${SCRIPT} "$@"
else
    `which ${SCRIPT}` "$@"
fi
