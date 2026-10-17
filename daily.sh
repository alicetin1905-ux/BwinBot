#!/usr/bin/env bash
# Daily job: settle finished slips, save today's slip, refresh slips_log.md and docs/data/*.json, push.
# Needs ODDS_API_KEY in the environment. Totals-only keeps it near 10-15 credits/day.
set -euo pipefail
cd "$(dirname "$0")"
: "${ODDS_API_KEY:?set ODDS_API_KEY}"

python3 bwinbot.py settle --api || echo "settle skipped (see message above)"
python3 bwinbot.py slip --api --bookmaker unibet_se --markets totals --save
python3 bwinbot.py log --out slips_log.md > /dev/null
python3 bwinbot.py export --out-dir docs/data

git add slips.sqlite slips_log.md docs
if git diff --cached --quiet; then
  echo "nothing new to commit"
else
  git commit -q -m "Daily slip log $(date -u +%F)"
  git push
fi
