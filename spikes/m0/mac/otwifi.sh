#!/bin/sh
# M0 spike: the Mac joining a hotspot from the command line (scenario S1, joiner role).
#   otwifi.sh join <ssid> <passphrase>   join it (no Location permission needed?)
#   otwifi.sh status                     address, gateway, and whether the name is visible
#   otwifi.sh leave <ssid>               forget it; macOS rejoins your usual Wi-Fi
# Joining a hotspot drops this Mac off its current Wi-Fi (and internet) until `leave`.
IF=$(networksetup -listallhardwareports | awk '/Wi-Fi/{getline; print $2}')
case "$1" in
  join)
    networksetup -setairportnetwork "$IF" "$2" "$3" && echo "join requested on $IF"
    sleep 5; "$0" status ;;
  status)
    echo "interface $IF: $(ipconfig getifaddr "$IF" || echo 'no address')"
    echo "gateway: $(route -n get default 2>/dev/null | awk '/gateway/{print $2}')"
    networksetup -getairportnetwork "$IF" ;;
  leave)
    networksetup -removepreferredwirelessnetwork "$IF" "$2"
    networksetup -setairportpower "$IF" off; sleep 2; networksetup -setairportpower "$IF" on
    sleep 8; "$0" status ;;
  *) sed -n '2,7p' "$0" ;;
esac
