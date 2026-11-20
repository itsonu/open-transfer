#!/bin/bash
# M0 spike: S1 driven by the app. The phone (USB, spike app in front) hosts a
# LocalOnlyHotspot; this Mac joins it with networksetup (no Location permission),
# measures, then leaves. The hotspot has no internet: everything runs here, over USB.
#   s1_app.sh <phone serial> <log file>
# Waits up to 5 min for the phone's own Mobile hotspot to be switched off first.
P=$1
LOG=$2
M0=$(dirname "$0")/..
export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"
IF=$(networksetup -listallhardwareports | awk '/Wi-Fi/{getline; print $2}')
SSID=""
say() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

front() { adb -s "$P" shell dumpsys window | grep -q "mCurrentFocus=.*opentransfer.m0/"; }
screen() {
  adb -s "$P" shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  adb -s "$P" shell cat /sdcard/ui.xml
}
tap() {  # tap a spike-app button by its text, scrolling to find it; never another app
  local label=$1 x1 y1 x2 y2
  for _ in 1 2 3 4 5 6; do
    front || { say "STOP: spike app not in front"; return 1; }
    read -r x1 y1 x2 y2 <<< "$(screen | tr '>' '\n' | grep -F "text=\"$label" | grep -o -E 'bounds="[^"]+"' | grep -o -E '[0-9]+' | tr '\n' ' ')"
    if [ -n "$y2" ] && [ "$y1" -gt 300 ] && [ "$y2" -lt 2200 ]; then
      adb -s "$P" shell input tap $(( (x1+x2)/2 )) $(( (y1+y2)/2 )); return 0
    elif [ -n "$y2" ] && [ "$y2" -ge 2200 ]; then
      adb -s "$P" shell input swipe 540 1500 540 900 200  # below the fold: scroll down
    else
      adb -s "$P" shell input swipe 540 700 540 1500 200  # above, or not found: scroll up
    fi
    sleep 1
  done
  say "button not found: $label"; return 1
}
applog() {  # the spike app's on-screen log (scrolled to the bottom)
  front && adb -s "$P" shell input swipe 540 1800 540 600 150
  screen | python3 -c 'import sys,re,html; m=re.findall(r"text=\"(OT M0 spike on[^\"]*)\"", sys.stdin.read()); print(html.unescape(m[0]) if m else "")'
}
cleanup() {
  [ -n "$SSID" ] && networksetup -removepreferredwirelessnetwork "$IF" "$SSID" >> "$LOG" 2>&1
  tap "   Stop hotspot" && say "asked the phone to stop the app hotspot"
  for _ in $(seq 1 30); do curl -s -m 3 -o /dev/null https://api.github.com && break; sleep 2; done
  say "back online: $(ipconfig getifaddr "$IF") gateway $(route -n get default 2>/dev/null | awk '/gateway/{print $2}')"
}
trap cleanup EXIT

adb -s "$P" get-state 2>/dev/null | grep -q device || { say "phone $P not connected over USB"; SSID=""; trap - EXIT; exit 1; }
say "waiting for the phone's Mobile hotspot to be switched off"
for _ in $(seq 1 150); do
  adb -s "$P" get-state 2>/dev/null | grep -q device || { say "phone dropped off USB"; trap - EXIT; exit 1; }
  adb -s "$P" shell ip -4 addr show ap0 2>/dev/null | grep -q inet || break
  sleep 2
done
adb -s "$P" shell ip -4 addr show ap0 2>/dev/null | grep -q inet && { say "Mobile hotspot still on after 5 min; giving up"; exit 1; }
say "Mobile hotspot is off; waiting for the spike app to be in front again"
for _ in $(seq 1 60); do front && break; sleep 2; done
front || { say "spike app not back in front after 2 min; stopping"; exit 1; }
sleep 2

tap "3. Host LocalOnlyHotspot" || exit 1
for _ in $(seq 1 15); do sleep 2; L=$(applog); echo "$L" | grep -q -E "HOTSPOT (UP|FAILED|REFUSED)" && break; done
echo "$L" | grep -E "HOTSPOT|interface" | while read -r line; do say "phone: $line"; done
SSID=$(echo "$L" | sed -n 's/.*HOTSPOT UP  ssid=\(.*\)  passphrase=.*/\1/p' | tail -1)
PASS=$(echo "$L" | sed -n 's/.*  passphrase=\(.*\)$/\1/p' | tail -1)
[ -z "$SSID" ] && { say "no hotspot name read; stopping"; exit 1; }

tap "5. TCP sink" || exit 1
sleep 1
say "Mac joining '$SSID' with networksetup (no Location permission)"
networksetup -setairportnetwork "$IF" "$SSID" "$PASS" >> "$LOG" 2>&1; say "networksetup exit $?"
for _ in $(seq 1 20); do GW=$(route -n get default 2>/dev/null | awk '/gateway/{print $2}'); ADDR=$(ipconfig getifaddr "$IF"); [ -n "$ADDR" ] && [ -n "$GW" ] && break; sleep 2; done
say "Mac on the app hotspot: address $ADDR gateway $GW"
python3 "$M0/tcp_speed.py" send "$GW" 200 >> "$LOG" 2>&1 || say "TCP send failed"
sleep 2
applog | grep -E "RECEIVED" | while read -r line; do say "phone: $line"; done
