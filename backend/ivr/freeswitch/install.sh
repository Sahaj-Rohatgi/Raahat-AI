#!/bin/bash
# Render the Raahat dialplan into FreeSWITCH's config and reload it.
#
# The dialplan needs three things that cannot be committed to the repo: the absolute path
# to this checkout (FreeSWITCH resolves prompt files by absolute path), the shared IVR key
# (a secret, read from backend/.env), and the backend's base URL. This fills all three into
# dialplan_98_raahat_ivr.xml.template and writes the result where FreeSWITCH reads it.
#
#     ./install.sh                                  # autodetect everything
#     API_BASE=http://127.0.0.1:8080 ./install.sh   # backend on another port
#     FS_CONF=/etc/freeswitch ./install.sh          # non-standard FreeSWITCH config dir
set -e
cd "$(dirname "$0")"

APP_DIR="$(cd ../../.. && pwd)"
TEMPLATE="dialplan_98_raahat_ivr.xml.template"
API_BASE="${API_BASE:-http://127.0.0.1:8001}"

[ -f "$TEMPLATE" ] || { echo "Missing $TEMPLATE next to this script." >&2; exit 1; }

# FreeSWITCH's config lives in a different place on Apple Silicon brew, Intel brew and Linux.
if [ -z "$FS_CONF" ]; then
  for candidate in /opt/homebrew/etc/freeswitch /usr/local/etc/freeswitch /etc/freeswitch; do
    [ -d "$candidate/dialplan/default" ] && FS_CONF="$candidate" && break
  done
fi
[ -n "$FS_CONF" ] || {
  echo "Could not find a FreeSWITCH config directory. Install FreeSWITCH first" >&2
  echo "(brew install freeswitch / apt install freeswitch), or set FS_CONF=/path/to/freeswitch." >&2
  exit 1
}

# The key must match what the backend checks; backend/.env is the source of truth.
IVR_KEY="$(sed -n 's/^IVR_API_KEY=//p' "$APP_DIR/backend/.env" 2>/dev/null | tail -1 | tr -d '"' | xargs || true)"
[ -n "$IVR_KEY" ] || {
  echo "No IVR_API_KEY found in $APP_DIR/backend/.env — the backend will reject every IVR call." >&2
  echo "Add one (any shared secret), then re-run: echo 'IVR_API_KEY=demo-ivr-key' >> backend/.env" >&2
  exit 1
}

for prompt in welcome ask_mood ask_sleep ask_safety ask_anxiety ask_pressure checkin_done sos_done invalid; do
  [ -f "$APP_DIR/backend/ivr/prompts/$prompt.wav" ] || {
    echo "Missing prompt $prompt.wav — run backend/ivr/prompts/generate.sh first." >&2
    exit 1
  }
done

TARGET="$FS_CONF/dialplan/default/98_raahat_ivr.xml"
# An earlier setup symlinked this straight at the repo; replace it with the rendered file.
[ -L "$TARGET" ] && rm -f "$TARGET"
sed -e "s#@@APP_DIR@@#${APP_DIR}#g" \
    -e "s#@@IVR_KEY@@#${IVR_KEY}#g" \
    -e "s#@@API_BASE@@#${API_BASE}#g" \
    "$TEMPLATE" > "$TARGET"

echo "Installed $TARGET"
echo "  project  : $APP_DIR"
echo "  backend  : $API_BASE"

if command -v fs_cli >/dev/null && fs_cli -x "status" >/dev/null 2>&1; then
  fs_cli -x "reloadxml" >/dev/null && echo "  reloaded : FreeSWITCH picked up the new dialplan"
else
  echo "  note     : FreeSWITCH is not running — start it (brew services start freeswitch)"
  echo "             or run 'fs_cli -x reloadxml' once it is, to load this dialplan."
fi

echo
echo "Dial 7100 from a softphone registered to this FreeSWITCH to reach the IVR."
