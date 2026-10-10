# ESP32-S3 mouse relay

The generator computer preloads a path over UART. The ESP32-S3 schedules standard
relative USB HID mouse reports to the receiving computer. No receiver application
or custom mouse driver is required for movement. The receiver's USB host supplies
the polling clock; firmware timestamps are dispatch deadlines, not guarantees of
host arrival time.

## Wiring

```text
Generator computer -- USB --> board's USB-to-UART bridge (control / flashing)
                             ESP32-S3
Receiving computer -- USB --> board's native USB / OTG port (HID mouse)

Optional normally-open stop button: GPIO4 -- button -- GND
```

The connected board was identified on COM3 as an ESP32-S3 rev v0.2, 16 MB quad
flash, 8 MB embedded PSRAM, behind a CH343 bridge. This identifies the chip and
memory, not the exact board model or its power circuit. Find the port marked USB,
OTG or native USB in its schematic; the UART connector cannot emit HID reports.
The S3's internal USB pins are **GPIO20 = D+**, **GPIO19 = D-**; see
[Espressif's USB device wiring](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/usb_device.html).
Use the board's native connector and a data-capable cable where available.

Many dual-port boards already connect UART0 to the bridge: GPIO44 is RX and
GPIO43 is TX. If yours has only a native USB connector, use a **3.3 V logic**
USB-to-UART adapter instead:

| Adapter | ESP32-S3 |
| --- | --- |
| TX | GPIO44 / RX |
| RX | GPIO43 / TX |
| GND | GND |
| VCC / 5V / 3V3 | Leave disconnected when native USB powers the board |

For an external adapter, hold BOOT, tap RESET, release BOOT to enter the loader
if automatic reset is unavailable. Keep GPIO19/20 reserved for USB. Change UART
and stop pins in `esp32_mouse/config.h` if the board uses different connections.
GPIO4 must be free; grounding it stops playback, and releasing it does not resume.

Verify that your board supports both USB power inputs at once before connecting
two hosts. The official
[DevKitC-1 supports both USB ports](https://docs.espressif.com/projects/esp-idf/en/v4.4.3/esp32s3/hw-reference/esp32s3/user-guide-devkitc-1.html#power-supply-options);
that does not establish a clone board's wiring. If unsure, use the external UART
adapter with VCC disconnected, and power only through the native USB connector.
Do not connect USB 5 V to a GPIO or wire two unisolated VBUS supplies together.

## Build and flash

Use Arduino-ESP32 **3.3.8**, the pinned core used for this implementation. In
Arduino IDE, add Espressif's board index URL in Preferences, install that core
in Boards Manager, and open `esp32_mouse/esp32_mouse.ino`. Select **ESP32S3 Dev
Module**, **USB-OTG (TinyUSB)**, and disable **USB CDC**, **MSC** and **DFU On
Boot**. Select 16 MB flash and OPI PSRAM for the connected chip; adjust memory
settings for other boards. Upload through the UART bridge.

With [Arduino CLI](https://docs.arduino.cc/arduino-cli/installation/), run from the
repo root (PowerShell):

```powershell
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.8 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
$mouseFqbn = 'esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB'
arduino-cli compile --fqbn $mouseFqbn --output-dir firmware/build firmware/esp32_mouse
arduino-cli upload --fqbn $mouseFqbn --port COM3 --input-dir firmware/build firmware/esp32_mouse
```

The local verification toolchain is isolated in `.tools/`: substitute
`.tools/arduino-cli/arduino-cli.exe` and add
`--config-file .tools/arduino-cli.yaml` to use it on this machine. This directory
is ignored by Git and is not required on another computer.

Before replacing existing firmware, back up the full flash using esptool:

```powershell
python -m pip install esptool
python -m esptool --port COM3 --baud 460800 read-flash 0 0x1000000 original-flash.bin
```

For this session the backup location is `.tools/esp32-s3-original-flash.bin`.
Keep a copy elsewhere if you remove `.tools`. To restore that **same board**,
disconnect it from the receiving computer, then run:

```powershell
python -m esptool --port COM3 --baud 460800 write-flash 0 .tools/esp32-s3-original-flash.bin
```

Restore overwrites flash contents; it does not change eFuses. Do not use another
board's backup. ROM download mode can expose a serial/JTAG identity on native
USB, unlike the application mouse. Flash/recover via UART with native USB
disconnected from the receiver when testing application enumeration.

## Replay generated paths

On the generator computer install `requirements-hardware.txt` (or just pyserial
for sending). The existing recorder keeps its dependency-free setup.

```powershell
python -m pip install -r requirements-hardware.txt
python generate.py 400 300 1200 700 --json > path.json
python replay.py --list-ports
python replay.py --port COM3 --probe
python replay.py --port COM3 --probe --identity
python replay.py examples/mouse_smoke.json --port COM3
python replay.py --port COM3 --circle --radius 120 --turns 3 --period 2
python replay.py path.json --dry-run
python replay.py path.json --port COM3
python replay.py path.json --port COM3 --clicks
```

`--probe` stops any active playback and queries firmware/USB readiness without
motion. `USB=1` means the HID endpoint is ready; `USB=0` means the receiving
USB connection is unavailable. The smoke example sends +10 X counts and then
-10 X counts, with no clicks; pointer processing can affect the return position.

The first path row is an anchor and sends no displacement. Place the receiving
cursor at the intended starting point yourself. `--clicks` enables generated
left clicks; by default, only motion is sent. Each enabled click has a 30 ms
press/release pair (`--click-hold-ms` changes the duration). Motion during that
hold is postponed. Ctrl+C sends STOP; losing controller contact during playback
aborts after 1.5 seconds and releases buttons when USB is available again.

**Pixels are not mouse counts.** This repo records desktop cursor positions after
OS pointer processing. Relative HID reports go through the receiver's pointer
speed/acceleration settings. Start with `--counts-per-pixel 1`, use movement-only
paths to measure displacement, and adjust the scale and receiver pointer settings.
A single scale cannot invert velocity-dependent acceleration. There is no
receiver feedback here, so exact endpoints and clicks on the requested target
are not guaranteed. The checkpoint's native poll jitter also need not survive
USB dispatch and the receiving OS unchanged.

Reports use a three-button mask and signed 8-bit relative X/Y and wheel, without
a report ID. The wire input report is four bytes; in boot protocol it is three
bytes (buttons/X/Y only; wheel is unavailable). Playback
supports 4096 reports and 60 seconds per loaded plan. Cumulative count rounding
preserves the scaled total displacement. Large deltas are split evenly into
reports bounded to +/-127 counts, preserving both axes' sums. For an enabled
click, only the last split motion report presses the button. Events closer than
10 ms are delayed to fit the advertised USB polling interval; the sender reports
how many source rows shifted. Splitting can stretch the path and change pointer
acceleration; it does not preserve exact pixel positions or arrival timing. The board
aborts rather than bursting reports if dispatch falls more than 20 ms behind or
a HID send fails. Completion reports the number of reports and largest dispatch
lateness, not receiver-observed timing. No commands are resumed automatically.

UART uses ASCII payloads framed as `payload*CCCC\n`, with CRC-16/CCITT-FALSE
(initial 0xFFFF, polynomial 0x1021); replies are plain ASCII. Commands are HELLO,
LOAD count, E index due_us dx dy buttons wheel, RUN, PING and IDENTITY. IDENTITY
returns the configured VID/PID, release, strings and USB interface profile over UART; receiver-side
enumeration is still needed to verify the host-visible fingerprint. A plain STOP line
is always accepted. Reports are loaded and acknowledged before RUN. Each event
must be ordered, in range and at least 10 ms after its predecessor; the last
report must release all buttons. CRC, parser, USB, watchdog or switch errors
discard the loaded plan. CRC detects transmission corruption, not authorization;
the UART connection is a trusted local control channel.

The UART handshake is now `OK HELLO 2 4096 10000 60000000 1500 USB=1` when
connected. Update both sender and firmware together; the sender refuses the
previous v1/16-bit firmware instead of uploading an incompatible plan.

## Check from the receiving computer

Copy `hid_check.py` to the receiving computer and install `hidapi==0.15.0`.
The checker is standalone and does not need the generator, models or PyTorch.
Use a commercial **USB** mouse as the reference; a laptop touchpad or Bluetooth
mouse has a different transport. Run both captures on the same OS, preferably
the same computer. Re-list after reconnecting devices: indexes can change.

```powershell
python -m pip install hidapi==0.15.0
python hid_check.py list
python hid_check.py snapshot --index 0 --output commercial.json
# Connect the ESP32-S3 native USB port, run list, and choose its current index.
python hid_check.py list
python hid_check.py snapshot --index 1 --output esp32.json
python hid_check.py compare commercial.json esp32.json --output comparison.json
```

Replace 0 and 1 with the actual indexes. Select usage page 1 / usage 2 (mouse).
If a product has multiple mouse collections, capture each relevant one; a
Windows snapshot's reconstructed report covers the selected collection only.

Snapshots include VID/PID, device release, strings, serial presence, bus type,
usage, interface number, report fingerprint, field sizes/ranges and relative
flags. Windows also reads the selected physical device's PnP subtree and driver
services and queries the selected USB device through its parent hub for
configuration/interface/endpoint descriptors, speed, EP0 size, release,
serial presence and extra USB strings. The hub query is guarded by VID/PID and
does not detach drivers or send SET/feature/output requests. Linux reads cached USB configuration, interface and
endpoint descriptors (including polling intervals) and speed through sysfs.
macOS captures HID identity and report descriptors where the OS permits access.
An access failure becomes a reported limitation; the checker never detaches
drivers or sends feature/output reports. Linux hidraw permission rules may be
needed for the HIDAPI fallback; a sysfs snapshot can often be read without them.

Comparison returns **observable_differences** when collected fingerprints differ,
or **inconclusive** otherwise. Exit status is 1 for differences, 0 for no observed
differences, and 2 for errors. Zero is not a certification of indistinguishability.
Windows reconstructed and Linux raw descriptors are not compared as identical
acquisition methods. Serial values and USB paths naturally distinguish two
individual commercial units too; these are reported separately or excluded.

This is a descriptor/identity check, not a behavioral classifier. It does not
measure motion distributions, raw arrival timing, idle behavior, USB request
responses, electrical characteristics or a particular application's detection.
Use receiver-side recordings for separate motion analysis, and account for
pointer settings and USB/OS timing when interpreting them.

## HID identity and visibility

Standard HID means a generic host driver can interpret the mouse reports; it
does not make every mouse's USB identity or behavior identical. See the
[USB-IF HID specification](https://www.usb.org/document-library/device-class-definition-hid-111).
The current user-selected identity test profile uses VID **0x046D**, PID
**0xC077**, manufacturer **Logitech**, and product **USB Optical Mouse**.
Logitech documents this VID/PID for the basic wired
[M105](https://support.logi.com/hc/de/articles/360023306434-M105-Technical-Specifications).
The identity is explicitly set in `esp32_mouse/config.h` before USB starts,
so the selected Arduino board's Espressif defaults cannot override it.
The [receiver investigation](../reports/2026-10-10-hid-investigation/README.md)
showed that changing only these strings and IDs left a readily distinguishable
TinyUSB interface. `usb_profile.h` now owns the pinned core's weak descriptor
callbacks; no installed core patch or special linker flags are needed. The
host-visible device has no serial (iSerialNumber = 0), no configuration/interface
strings, one 4-byte interrupt IN endpoint at 0x81 with a 10 ms interval, and
boot mouse subclass/protocol 1/2. There is no interrupt OUT endpoint.
`GET_REPORT` returns button state with zero relative deltas, so querying state
cannot repeat movement; boot protocol drops the wheel byte. Nonzero `SET_IDLE`
rates repeat stationary state while idle, without replaying prior motion.

Release 0x7200 and the endpoint/interface choices follow the comparison
[C077 capture](https://forums.developer.nvidia.com/t/nano-configfs-custom-usb-mouse-device-works-for-a-windows-host-but-not-a-linux-host/190998).
This is one hardware revision, not a specification for every M105. That capture
does not contain the raw report descriptor. Our 52-byte standard descriptor is
authored, not cloned; the capture advertises 46 bytes. These lengths are different.
Use `replay.py --port COM3 --probe --identity` to inspect the configured values.

This is still not an indistinguishable M105 clone. The commercial sample is
low-speed (1.5 Mbps) with 8-byte EP0; this S3 build remains full-speed (12 Mbps)
with the pinned stack's actual 64-byte EP0. Advertising a smaller EP0 without
rebuilding/reconfiguring the stack would misdescribe its transfer behavior.
USB request responses, report bytes, electrical behavior, idle/motion timing and
firmware can also differ. Replug native USB after flashing to force fresh receiver
enumeration, then take a new snapshot with the updated checker on the receiving
computer. Previously saved snapshots do not update themselves. Older Windows
snapshots lack hub-level evidence; missing evidence is reported as unknown rather
than treated as a device difference.

The previous project profile can be restored in `config.h` with VID 0x303A,
PID 0x1001, manufacturer `Mouse Generator Project` and product
`Mouse Generator Relay`, then recompiling and flashing. VID ownership and PID
assignment are explained by [USB-IF](https://www.usb.org/getting-vendor-id);
public catalog entries are not an assignment to this project for distribution.

The generic Arduino `USBHID` wrapper is deliberately not linked in this build;
its default IN/OUT endpoints and TinyUSB strings are no longer the application
descriptors. The USB stack itself is still TinyUSB; removing its strings is not
authentication or proof of commercial origin. Firmware makes no claim of
universal indistinguishability.

Run `python -m unittest test_hardware -v` for count conversion, framing,
transport failure handling, report layout and comparison semantics.
