#!/usr/bin/env bash
# Deploy Black Wire Forge to your always-on box.
#
# WHY an always-on box: the control plane must outlive what it controls. A ComfyUI
# lane is usually on-demand; a UI living on one of them is down exactly when you
# open it to start things. Put this app on whatever box in your fleet is always on.
#
# config.json is NOT copied. It carries machine addresses and differs per host --
# a lane's "host" may be 127.0.0.1 from the box that runs it, and a LAN IP from
# anywhere else. Deploying one box's config to another silently gives you a lane
# that never comes up. Edit the remote copy, or keep a per-host one.
set -euo pipefail
HOST="${1:?usage: deploy.sh <ssh-host> [dest]}"   # ssh alias, never a raw IP
DEST="${2:-~/black-wire-forge}"
cd "$(dirname "$0")/.."
ssh "$HOST" "mkdir -p $DEST"
rsync -a --exclude '.git' --exclude 'config.json' --exclude '__pycache__' \
      --exclude 'data' --exclude 'outputs' ./ "$HOST:$DEST/"
echo "synced to $HOST:$DEST (config.json deliberately not copied)"
ssh "$HOST" "cd $DEST && python3 -c 'import ast;ast.parse(open(\"server.py\").read())'" \
  && echo "server.py parses on $HOST"
echo
echo "start it:   ssh $HOST 'cd $DEST && nohup python3 server.py > /tmp/bwf.log 2>&1 &'"
echo "check it:   curl -s -o /dev/null -w '%{http_code}\\n' http://$HOST:3998/"
