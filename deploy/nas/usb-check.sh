#!/bin/sh
# Run on the NAS over SSH, with the T-Beam plugged in:  sudo sh deploy/nas/usb-check.sh
# Reports whether DSM exposes the T-Beam as a serial device.
echo "== USB devices (look for 10c4:ea60 = CP210x, 1a86:55d4 = CH9102, 1a86:7523 = CH340)"
for d in /sys/bus/usb/devices/*; do
  [ -f "$d/idVendor" ] && echo "$(cat "$d/idVendor"):$(cat "$d/idProduct") $(cat "$d/product" 2>/dev/null)"
done
echo "== serial device nodes"
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "none"
echo "== loaded serial drivers"
lsmod | grep -E 'usbserial|cp210x|ch341|ch343|cdc_acm' || echo "none loaded"
echo "== drivers available on this DSM"
find /lib/modules -name '*.ko' 2>/dev/null | grep -E 'usbserial|cp210x|ch341|ch343|cdc-acm' || echo "none found"
cat <<'EOF'

If a ttyUSB/ttyACM node exists, put it in .env as VB_MESH_DEV=/dev/ttyUSB0 (or ttyACM0).
If not, but a driver is listed as available:  insmod /lib/modules/usbserial.ko; insmod /lib/modules/<driver>.ko
and add the same lines to a boot-up Triggered Task (Control Panel -> Task Scheduler, user root).
If no driver exists at all, open an issue with the output above.
EOF
