#!/usr/bin/env bash
# Cron safety net: fires every minute and restarts the loop if it died.
# The crontab line itself is owned by `upwork-cron start` / `stop`. This
# script only ensures the loop is alive and does NOT touch the schedule,
# so a deliberate `stop` stays stopped.
exec /home/logisite/.local/bin/upwork-cron ensure