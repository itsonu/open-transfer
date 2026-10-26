#!/usr/bin/env bash
# Runs inside reactivecircus/android-emulator-runner (which executes `script:` one line
# at a time, so the real logic lives here). Usage: emulator_smoke.sh path/to/app.apk
set -euo pipefail

APK="${1:-open-transfer-android.apk}"
PKG="io.github.itsonu.opentransfer"
PORT=5000

dump_logs() {
  echo "::group::logcat (Python / Open Transfer / crashes)"
  adb logcat -d | grep -iE "python|open_transfer|opentransfer|chaquopy|AndroidRuntime" | tail -300 || true
  echo "::endgroup::"
}
trap 'status=$?; if [ "$status" -ne 0 ]; then echo "Smoke test failed (exit $status)"; dump_logs; fi' EXIT

adb wait-for-device
adb logcat -c || true

echo "Installing $APK"
# -g grants every runtime permission the app asks for, so no dialog blocks the start.
adb install -r -g "$APK"
adb shell pm grant "$PKG" android.permission.POST_NOTIFICATIONS || true

echo "Launching the app"
adb shell am start -W -n "$PKG/.MainActivity"
adb forward "tcp:$PORT" "tcp:$PORT"

echo "Waiting for the Python server (the first start extracts Python; be patient)"
deadline=$((SECONDS + 180))
until curl -sf --max-time 3 "http://127.0.0.1:$PORT/api/health"; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "The server never answered on port $PORT"
    adb shell dumpsys activity services "$PKG" | head -60 || true
    exit 1
  fi
  sleep 2
done
echo

state="$(curl -sf --max-time 10 "http://127.0.0.1:$PORT/api/state")"
echo "$state" | head -c 2000
echo
case "$state" in
  *'"platform":"android"'*) echo "State reports platform android: OK" ;;
  *) echo 'Expected "platform":"android" in /api/state'; exit 1 ;;
esac

echo "Real transfer: phone -> CI runner"
python android/ci/smoke_transfer.py

# The share sheet lists the apps whose activities resolve the intent.
offered() { # action mime activity
  adb shell "cmd package query-activities --components -a '$1' -t '$2'" | tr -d '\r' \
    | grep -E "(^|[[:space:]])$PKG/(\.|$PKG\.)$3([[:space:]]|\$)" >/dev/null
}
echo "Share sheet: offered for photos, videos and files"
for type in image/jpeg image/png video/mp4 audio/mpeg application/pdf application/zip text/csv; do
  offered android.intent.action.SEND "$type" MainActivity || { echo "Not offered for $type"; exit 1; }
done
offered android.intent.action.SEND_MULTIPLE "image/*" MainActivity || { echo "Not offered for several photos"; exit 1; }
offered android.intent.action.SEND text/plain ClipboardSendActivity || { echo "Not offered for text"; exit 1; }
if offered android.intent.action.SEND text/plain MainActivity; then
  echo "Text would show Open Transfer twice in the share sheet"; exit 1
fi

echo "Sharing a photo hands it to the app"
adb logcat -c || true
adb shell am start -W -a android.intent.action.SEND -t image/jpeg \
  --eu android.intent.extra.STREAM "content://media/external/images/media/1" -n "$PKG/.MainActivity"
deadline=$((SECONDS + 20))
until adb logcat -d -s OpenTransfer:I | grep "Shared 1 file" >/dev/null; do
  if [ "$SECONDS" -ge "$deadline" ]; then echo "The app never took the shared photo"; exit 1; fi
  sleep 1
done
adb shell pidof "$PKG" >/dev/null || { echo "The app died after the share"; exit 1; }
curl -sf --max-time 5 "http://127.0.0.1:$PORT/api/health" >/dev/null || { echo "Server gone after the share"; exit 1; }

echo "Smoke test passed"
