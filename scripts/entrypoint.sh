#!/bin/sh
mkdir -p /app/data /app/logs
[ -f /app/data/events.csv ] || cp /app/defaults/events.csv /app/data/events.csv
exec "$@"
