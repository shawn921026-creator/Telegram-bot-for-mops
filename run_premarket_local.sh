#!/bin/bash
# 本機備援（launchd 08:12）：先同步 GitHub 上的紀錄，今天還沒發才跑，跑完把紀錄推回去
cd "$(dirname "$0")" || exit 1
for i in $(seq 1 24); do   # 剛從睡眠喚醒時網路可能還沒好，最多等 2 分鐘
  /usr/bin/host -W 3 api.github.com >/dev/null 2>&1 && break
  sleep 5
done
git pull -q --rebase --autostash
PREMARKET_AUTO=1 /usr/bin/python3 premarket.py
git add premarket_state.json holidays.json
git diff --cached --quiet || { git commit -q -m "premarket state (local backup)" && git pull -q --rebase --autostash && git push -q; }
