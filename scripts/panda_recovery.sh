#!/usr/bin/env sh

DFU_UTIL="dfu-util"

STANDARD_FW="/data/openpilot/panda/board/obj/panda.bin.signed"
TESTING_FW="/data/openpilot/panda/board/obj/panda.bin.testing.signed"
ATL=`cat /data/params/d/dp_atl`

/data/openpilot/selfdrive/ui/qt/spinner &
python -c "from panda import Panda; Panda().reset(enter_bootstub=True); Panda().reset(enter_bootloader=True)" || true
sleep 1
FW=$STANDARD_FW
if [ -f /data/params/d/dp_atl ] && [ $ATL != "0" ]; then
  if [ -f /data/openpilot/panda/board/obj/panda.bin.testing.signed ]; then
    echo "Use testing firmware..."
    FW=$TESTING_FW
  else
    echo "Missing testing firmware, use standard firmware instead..."
  fi
else
  echo "Use standard firmware..."
fi
echo "\n\n\nUpdating panda.bin..."
$DFU_UTIL -d 0483:df11 -a 0 -s 0x08004000 -D $FW
echo "\n\n\nUpdating bootstub.panda.bin..."
$DFU_UTIL -d 0483:df11 -a 0 -s 0x08000000:leave -D /data/openpilot/panda/board/obj/bootstub.panda.bin
sleep 1


killall spinner &
reboot
