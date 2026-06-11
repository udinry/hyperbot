#!/usr/bin/env bash
# One-shot deploy of the validated trend stack (paper/observer mode) on the VPS.
# Safe by design: nothing here places real orders — trend_bot runs without
# --live and forward_test is paper-only. Run as: bash deploy_trend_stack.sh
set -euo pipefail

REPO=/opt/hyperbot
BRANCH=claude/hyperbot-code-audit-vtbvod

cd "$REPO"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull origin "$BRANCH"

# deps (idempotent)
"$REPO/.venv/bin/pip" install -q -r ai_trader/requirements.txt

# run the test suites before enabling anything
"$REPO/.venv/bin/python" -m pytest hft_bot/tests/ -q
( cd ai_trader && "$REPO/.venv/bin/python" -m pytest tests/ -q )

# install + enable the always-on pieces (forward test timer + trend bot observer)
sudo cp live_server_config/hyperbot-forward-test.service /etc/systemd/system/
sudo cp live_server_config/hyperbot-forward-test.timer   /etc/systemd/system/
sudo cp live_server_config/hyperbot-trend.service        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperbot-forward-test.timer
sudo systemctl enable --now hyperbot-trend

echo
echo "Deployed. Verify with:"
echo "  systemctl list-timers | grep hyperbot"
echo "  journalctl -u hyperbot-trend -n 20"
echo "  cd $REPO/hft_bot && $REPO/.venv/bin/python forward_test.py --report"
echo
echo "NOTE: this stack is PAPER/OBSERVER only. Going live later requires"
echo "PRIVATE_KEY in $REPO/.env AND editing hyperbot-trend.service — see"
echo "hft_bot/STRATEGY_V2.md 'Deployment' and the drift-check gate first."
