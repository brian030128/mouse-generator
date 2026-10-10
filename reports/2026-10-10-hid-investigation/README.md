# Logitech mouse identity investigation

On 10 October 2026, checks on Windows 11 identified the wired device claiming to be a Logitech USB Optical Mouse (`046D:C077`) as very likely custom hardware impersonating a Logitech mouse. The Bluetooth MX Master 3 (`046D:B023`) showed no comparable inconsistency. This is an evidence-based assessment, not cryptographic authentication or proof of malicious intent. The pan1080xa3 device was excluded from the investigation at the user's request.

## Findings for the wired mouse

The strongest evidence came from USB descriptor queries through the Windows hub driver, beyond the original HID fingerprint script.

| Property | Observed value |
| --- | --- |
| Manufacturer and product | `Logitech / USB Optical Mouse` |
| Vendor and product IDs | `046D:C077` |
| USB interface string | `TinyUSB HID` |
| USB configuration string | `TinyUSB Device` |
| Serial number | `000000000001` |
| Device release | `0x0100` |
| Control endpoint maximum packet size | 64 bytes |
| Interrupt endpoints | OUT `0x01` and IN `0x81`, each 64 bytes |
| Advertised interrupt interval | 1 ms at the reported full speed; not a measured event rate |
| HID interface subclass and protocol | Both zero |
| HID report | ID 1; three button bits; signed 16-bit relative X/Y; signed 8-bit wheel |
| Windows location | `Port_#0001.Hub_#0001` |

The TinyUSB names, combined with the other discrepancies, strongly support a custom implementation presenting a Logitech identity. [TinyUSB](https://github.com/hathach/tinyusb) is an open-source USB stack for embedded systems. These strings alone do not identify the board or prove which firmware is executing.

A [firsthand C077 mouse capture published on NVIDIA's developer forum](https://forums.developer.nvidia.com/t/nano-configfs-custom-usb-mouse-device-works-for-a-windows-host-but-not-a-linux-host/190998) reports no serial number, an 8-byte control endpoint, one 4-byte interrupt IN endpoint with a 10 ms interval, and boot mouse subclass/protocol values of 1/2. That capture is a comparison sample, not a manufacturer specification covering every hardware revision.

The local `firmware/esp32_mouse/esp32_mouse.ino` uses a similar report layout: report ID 1, signed 16-bit X/Y, and an 8-bit wheel. It declares five button bits whereas the attached device declares three. This resemblance does not establish that the attached device runs that source or uses an ESP32.

## Findings for the MX Master 3

The MX Master 3 exposes a Bluetooth LE HID connection with mouse, keyboard, and vendor collections. Its mouse descriptor includes 12-bit relative X/Y and vertical and horizontal scrolling fields. Windows reports signed Microsoft HID drivers and a signed Logitech Download Assistant driver associated with its collections.

The `046D:B023` identity is consistent with [Solaar's MX Master 3 device capture](https://raw.githubusercontent.com/pwr-Solaar/Solaar/master/docs/devices/MX%20Master%203%20Wireless%20Mouse%20B023.txt). No comparable identity inconsistency was found in these checks. Driver signatures authenticate driver packages, not the physical mouse; this device has not been proven genuine.

## Evidence and scope

The investigation enumerated HID devices, captured both Logitech mouse collections, inspected Windows PnP properties and signed-driver records, read USB device/configuration/string descriptors from the wired device, and compared the results with published captures. Device firmware and configuration were not changed.

- [Wired mouse HID snapshot](logitech_usb.json)
- [MX Master 3 HID snapshot](mx_master_3.json)
- [USB hub descriptor results](usb_probe.json), including the decisive TinyUSB strings
- [Windows PnP and driver records](windows_evidence.json)
- [Read-only USB probe](usb_probe.py), fixed to the captured hub and port and guarded by a VID/PID check; it is a diagnostic artifact for this machine, not a general device scanner

Windows HIDAPI reconstructs HID report descriptors, so those captures are not original byte-for-byte USB report descriptors. The separate hub query collected USB device and configuration descriptors and strings. No physical inspection, firmware extraction, electrical analysis, movement timing measurement, or Logitech HID++ challenge was performed. The results cannot establish the exact board, its operator's intent, or authenticity against a fully imitated implementation.

The practical conclusion is that the wired `046D:C077` device is the likely spoofer. Mapping it to a physical device by unplugging it and observing its disappearance would confirm which attached object owns this identity.
