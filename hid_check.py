"""Read-only HID fingerprints and comparison; run on the receiving computer.

python hid_check.py list
python hid_check.py snapshot --index 0 --output commercial.json
python hid_check.py snapshot --index 1 --output esp32.json
python hid_check.py compare commercial.json esp32.json

Comparisons show observable differences, not whether a device is commercial,
human-operated, or indistinguishable to an arbitrary application.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys

SCHEMA = 1
IDENTITY_KEYS = ("vendor_id", "product_id", "release_number", "manufacturer_string",
                 "product_string", "usage_page", "usage", "interface_number", "bus_type")


def descriptor_summary(data):
    """Decode report sizes and field properties from HID short items.

    This is a fingerprint summary, not a full USB/HID conformance validator.
    """
    state = {"page": 0, "size": 0, "count": 0, "id": 0, "min": 0, "max": 0}
    stack, usages, fields, collections, bits = [], [], [], [], {}
    usage_min = usage_max = None
    i = 0
    while i < len(data):
        prefix = data[i]
        i += 1
        if prefix == 0xFE:
            if i + 2 > len(data) or i + 2 + data[i] > len(data):
                raise ValueError("truncated HID long item")
            i += 2 + data[i]
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        if i + size > len(data):
            raise ValueError("truncated HID short item")
        raw = data[i:i + size]
        value = int.from_bytes(raw, "little")
        signed = int.from_bytes(raw, "little", signed=True)
        i += size
        kind, tag = (prefix >> 2) & 3, prefix >> 4
        if kind == 1:
            if tag in (0, 7, 8, 9):
                state[{0: "page", 7: "size", 8: "id", 9: "count"}[tag]] = value
            elif tag == 1:
                state["min"] = signed
            elif tag == 2:
                state["max"] = signed if state["min"] < 0 else value
            elif tag == 10:
                stack.append(state.copy())
            elif tag == 11:
                if not stack:
                    raise ValueError("HID global stack underflow")
                state = stack.pop()
        elif kind == 2:
            if tag == 0:
                usages.append(value)
            elif tag == 1:
                usage_min = value
            elif tag == 2:
                usage_max = value
        elif kind == 0:
            if tag == 10:
                collections.append({"type": value, "usage_page": state["page"],
                                    "usages": usages.copy()})
            if tag in (8, 9, 11):
                report_type = {8: "input", 9: "output", 11: "feature"}[tag]
                key = (report_type, state["id"])
                bits[key] = bits.get(key, 0) + state["size"] * state["count"]
                fields.append({"type": report_type, "report_id": state["id"],
                               "usage_page": state["page"], "usages": usages.copy(),
                               "usage_min": usage_min, "usage_max": usage_max,
                               "bits": state["size"], "count": state["count"],
                               "logical_min": state["min"], "logical_max": state["max"],
                               "constant": bool(value & 1), "relative": bool(value & 4),
                               "flags": value})
            usages = []
            usage_min = usage_max = None
    reports = [{"type": kind, "report_id": rid, "payload_bits": count,
                "wire_bytes": (count + 7) // 8 + bool(rid)}
               for (kind, rid), count in sorted(bits.items())]
    return {"collections": collections, "reports": reports, "fields": fields}


def usb_descriptor_summary(data):
    """Read cached Linux USB device/configuration/endpoint descriptors."""
    result = {"interfaces": [], "endpoints": [], "configurations": []}
    i, interface = 0, None
    while i < len(data):
        if i + 2 > len(data) or data[i] < 2 or i + data[i] > len(data):
            raise ValueError("truncated USB descriptor")
        d = data[i:i + data[i]]
        dtype = d[1]
        if dtype == 1 and len(d) >= 18:
            result["device"] = {"bcd_usb": int.from_bytes(d[2:4], "little"),
                                "class": d[4], "subclass": d[5], "protocol": d[6],
                                "ep0_max_packet": d[7], "config_count": d[17]}
        elif dtype == 2 and len(d) >= 9:
            result["configurations"].append({"value": d[5], "interface_count": d[4],
                                              "attributes": d[7], "max_power_ma": d[8] * 2})
        elif dtype == 4 and len(d) >= 9:
            interface = d[2]
            result["interfaces"].append({"number": interface, "alternate": d[3],
                                         "endpoints": d[4], "class": d[5],
                                         "subclass": d[6], "protocol": d[7]})
        elif dtype == 5 and len(d) >= 7:
            result["endpoints"].append({"interface": interface, "address": d[2],
                                        "attributes": d[3],
                                        "max_packet": int.from_bytes(d[4:6], "little"),
                                        "interval": d[6]})
        i += len(d)
    return result


def linux_details(path):
    node = Path("/sys/class/hidraw") / Path(path).name / "device"
    node = node.resolve(strict=True)
    descriptor = (node / "report_descriptor").read_bytes()
    usb = next((p for p in (node, *node.parents) if (p / "idVendor").exists()), None)
    topology = None
    if usb:
        topology = usb_descriptor_summary((usb / "descriptors").read_bytes())
        topology["speed_mbps"] = (usb / "speed").read_text().strip()
    return descriptor, topology


def windows_topology(path):
    import ctypes as c
    from ctypes import wintypes as w
    import winreg

    # Traverse the selected physical device's tree with native Configuration
    # Manager calls. Do not group unrelated units just because VID/PID match.
    parts = path.split("#")
    if len(parts) < 3:
        raise ValueError("unexpected Windows HID path")
    instance = "\\".join((parts[0].removeprefix("\\\\?\\"), parts[1], parts[2]))
    prefix = re.match(r"VID_[0-9A-F]{4}&PID_[0-9A-F]{4}|[^&]+", parts[1], re.I)
    if not prefix:
        raise ValueError("unexpected Windows hardware ID")
    cm = c.WinDLL("cfgmgr32")
    cm.CM_Locate_DevNodeW.argtypes = [c.POINTER(w.DWORD), w.LPWSTR, w.ULONG]
    cm.CM_Locate_DevNodeW.restype = w.ULONG
    cm.CM_Get_Device_IDW.argtypes = [w.DWORD, w.LPWSTR, w.ULONG, w.ULONG]
    cm.CM_Get_Device_IDW.restype = w.ULONG
    for name in ("CM_Get_Parent", "CM_Get_Child", "CM_Get_Sibling"):
        fn = getattr(cm, name)
        fn.argtypes = [c.POINTER(w.DWORD), w.DWORD, w.ULONG]
        fn.restype = w.ULONG
    root = w.DWORD()
    if cm.CM_Locate_DevNodeW(c.byref(root), instance, 0):
        raise OSError("cannot locate selected PnP node")

    def instance_id(node):
        buffer = c.create_unicode_buffer(512)
        if cm.CM_Get_Device_IDW(node, buffer, len(buffer), 0):
            raise OSError("cannot read PnP instance ID")
        return buffer.value

    for _ in range(8):
        parent = w.DWORD()
        if cm.CM_Get_Parent(c.byref(parent), root, 0):
            break
        parent_id = instance_id(parent)
        if prefix[0].upper() not in parent_id.upper():
            break
        root = parent
        if parent_id.upper().startswith("USB\\") and "&MI_" not in parent_id.upper():
            break
    root_id = instance_id(root)
    usb_address = None
    rows, pending, visited = [], [root.value], set()
    while pending:
        node = pending.pop()
        if node in visited or len(visited) >= 128:
            raise ValueError("unexpected PnP tree size or cycle")
        visited.add(node)
        node_id = instance_id(node)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            "SYSTEM\\CurrentControlSet\\Enum\\" + node_id) as key:
            def value(name):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return None
            rows.append({"class_guid": value("ClassGUID"), "service": value("Service"),
                         "name": value("FriendlyName") or value("DeviceDesc")})
            if node == root.value:
                usb_address = value("Address")
        child = w.DWORD()
        if not cm.CM_Get_Child(c.byref(child), node, 0):
            while True:
                pending.append(child.value)
                sibling = w.DWORD()
                if cm.CM_Get_Sibling(c.byref(sibling), child, 0):
                    break
                child = sibling
    result = {"pnp_nodes": sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))}
    usb_id = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", root_id, re.I)
    if usb_id:
        try:
            hub = w.DWORD()
            if cm.CM_Get_Parent(c.byref(hub), root, 0):
                raise OSError("cannot locate USB parent hub")
            result["usb"] = windows_usb_descriptors(instance_id(hub), usb_address,
                                                    int(usb_id[1], 16), int(usb_id[2], 16))
        except (OSError, ValueError) as exc:
            result["usb_query_error"] = str(exc)
    return result


def windows_usb_descriptors(hub_id, port, vid, pid):
    """Read only the selected USB device via its parent hub, guarded by VID/PID.

    No driver detachment, SET requests, feature reports or hub-port writes.
    """
    import ctypes as c
    from ctypes import wintypes as w
    import struct
    import uuid

    if not isinstance(port, int) or not 1 <= port <= 255:
        raise ValueError("selected device has no usable USB hub-port address")
    cm = c.WinDLL("cfgmgr32")
    k = c.WinDLL("kernel32", use_last_error=True)
    guid = (c.c_byte * 16).from_buffer_copy(uuid.UUID("f18a0e88-c30c-11d0-8815-00a0c906bed8").bytes_le)
    cm.CM_Get_Device_Interface_List_SizeW.argtypes = [c.POINTER(w.ULONG), c.c_void_p, w.LPCWSTR, w.ULONG]
    cm.CM_Get_Device_Interface_ListW.argtypes = [c.c_void_p, w.LPCWSTR, w.LPWSTR, w.ULONG, w.ULONG]
    size = w.ULONG()
    if cm.CM_Get_Device_Interface_List_SizeW(c.byref(size), guid, hub_id, 0) or not size.value:
        raise OSError("cannot resolve selected parent hub interface")
    paths = c.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_Interface_ListW(guid, hub_id, paths, size.value, 0) or not paths.value:
        raise OSError("cannot read selected parent hub interface")
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.c_void_p,
                                 w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    k.DeviceIoControl.restype = w.BOOL
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.CreateFileW(paths.value, 0x40000000, 3, None, 3, 0, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())

    def query(code, payload, length):
        buf = c.create_string_buffer(length)
        c.memmove(buf, payload, len(payload))
        used = w.DWORD()
        if not k.DeviceIoControl(handle, code, buf, length, buf, length, c.byref(used), None):
            raise c.WinError(c.get_last_error())
        return buf.raw[:used.value]

    def descriptor(kind, index=0, language=0, length=255):
        request = struct.pack("<IBBHHH", port, 0x80, 6, (kind << 8) | index, language, length)
        return query(0x220410, request, 12 + length)[12:]

    try:
        info = query(0x220448, struct.pack("<I", port), 4096)
        if len(info) < 24:
            raise ValueError("truncated USB connection information")
        device = info[4:22]
        if device[:2] != b"\x12\x01" or (int.from_bytes(device[8:10], "little"),
                                                   int.from_bytes(device[10:12], "little")) != (vid, pid):
            raise ValueError("selected hub-port identity changed; query stopped")
        header = descriptor(2, length=9)
        if len(header) != 9 or header[:2] != b"\x09\x02":
            raise ValueError("invalid USB configuration header")
        length = int.from_bytes(header[2:4], "little")
        if not 9 <= length <= 4096:
            raise ValueError("unsupported USB configuration length")
        config = descriptor(2, length=length)
        if len(config) != length:
            raise ValueError("truncated USB configuration")
        return usb_hub_summary(device, config, info[23], descriptor)
    finally:
        k.CloseHandle(handle)


def usb_hub_summary(device, config, speed_code, get_descriptor):
    """Decode hub-query evidence, with strings but not individual serial values."""
    if len(device) != 18 or len(config) < 9:
        raise ValueError("truncated hub device/configuration descriptor")
    result = usb_descriptor_summary(device + config)
    result["speed_mbps"] = {0: "1.5", 1: "12", 2: "480", 3: "5000"}.get(speed_code)
    result["device"].update({"release_number": int.from_bytes(device[12:14], "little"),
                              "serial_present": bool(device[16])})
    indices = {"manufacturer": device[14], "product": device[15], "configuration": config[6]}
    position = 0
    while position < len(config):
        item = config[position:position + config[position]]
        if item[1] == 4:
            if len(item) < 9:
                raise ValueError("truncated USB interface descriptor")
            indices[f"interface_{item[2]}_{item[3]}"] = item[8]
        position += len(item)
    result["strings"] = {}
    for name, index in indices.items():
        if not index:
            result["strings"][name] = None
            continue
        raw = get_descriptor(3, index, 0x0409)
        if len(raw) < 2 or raw[1] != 3 or raw[0] < 2 or raw[0] > len(raw) or raw[0] % 2:
            raise ValueError(f"invalid USB string descriptor: {name}")
        result["strings"][name] = raw[2:raw[0]].decode("utf-16-le")
    return result


def enumerate_devices():
    import hid
    # Mice first, then other HID collections (needed to expose extra interfaces).
    return sorted(hid.enumerate(), key=lambda d: (not (d.get("usage_page") == 1 and
                                                     d.get("usage") == 2), d["path"]))


def snapshot(device):
    result = {"schema": SCHEMA, "captured_utc": datetime.now(timezone.utc).isoformat(),
              "host": {"system": platform.system(), "release": platform.release()},
              "identity": {k: device.get(k) for k in IDENTITY_KEYS},
              "serial_number": device.get("serial_number"),
              "path": os.fsdecode(device["path"]), "descriptor": None,
              "topology": None, "limitations": [
                  "No motion, timing, firmware, USB electrical or application-specific detection test.",
                  "A difference identifies two fingerprints; it does not classify authenticity."]}
    data = None
    if sys.platform == "linux":
        try:
            data, result["topology"] = linux_details(result["path"])
            source = "linux-sysfs-raw"
        except (OSError, ValueError) as exc:
            result["limitations"].append(f"Linux sysfs unavailable: {exc}")
    if data is None:
        import hid
        handle = hid.device()
        try:
            handle.open_path(device["path"])
            data = bytes(handle.get_report_descriptor())
            source = "windows-reconstructed" if sys.platform == "win32" else "hidapi-raw"
        except (OSError, ValueError, AttributeError) as exc:
            result["limitations"].append(f"HID descriptor unavailable: {exc}")
        finally:
            handle.close()
    if data:
        result["descriptor"] = {"source": source, "bytes": len(data), "hex": data.hex(),
                                "sha256": hashlib.sha256(data).hexdigest()}
        try:
            result["descriptor"]["summary"] = descriptor_summary(data)
        except ValueError as exc:
            result["limitations"].append(f"Descriptor summary unavailable: {exc}")
    if sys.platform == "win32":
        result["limitations"].append("Windows HIDAPI reconstructs the selected collection's descriptor; "
                                      "it is not the original full USB report descriptor.")
        try:
            result["topology"] = windows_topology(result["path"])
            error = result["topology"].pop("usb_query_error", None)
            if error:
                result["limitations"].append(f"USB hub descriptor query unavailable: {error}")
        except (OSError, ValueError) as exc:
            result["limitations"].append(f"PnP sibling query unavailable ({type(exc).__name__}).")
        if not result["topology"] or "usb" not in result["topology"]:
            result["limitations"].append("USB endpoint intervals, speed, power and extra strings unavailable.")
    elif sys.platform != "linux":
        result["limitations"].append("USB endpoint topology is unavailable on this platform.")
    return result


def compare(reference, candidate):
    for label, doc in (("reference", reference), ("candidate", candidate)):
        if doc.get("schema") != SCHEMA or not isinstance(doc.get("identity"), dict):
            raise ValueError(f"{label}: unsupported or malformed snapshot")
    differences, unknown = [], []

    def check(name, left, right):
        if left is None or right is None:
            unknown.append(name)
        elif left != right:
            differences.append({"field": name, "reference": left, "candidate": right})

    for key in IDENTITY_KEYS:
        check(f"identity.{key}", reference["identity"].get(key), candidate["identity"].get(key))
    check("serial_present", bool(reference.get("serial_number")), bool(candidate.get("serial_number")))
    a, b = reference.get("descriptor"), candidate.get("descriptor")
    if a and b and a.get("source") == b.get("source"):
        check("descriptor.sha256", a.get("sha256"), b.get("sha256"))
        check("descriptor.summary", a.get("summary"), b.get("summary"))
    else:
        unknown.append("descriptor (unavailable or different acquisition methods)")
    if reference.get("host", {}).get("system") == candidate.get("host", {}).get("system"):
        a, b = reference.get("topology"), candidate.get("topology")
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                check(f"topology.{key}", a.get(key), b.get(key))
        else:
            unknown.append("topology (unavailable)")
    else:
        unknown.append("topology (different host platforms)")
    return {"verdict": "observable_differences" if differences else "inconclusive",
            "differences": differences, "unknown": unknown,
            "unit_serials_differ": reference.get("serial_number") != candidate.get("serial_number"),
            "interpretation": "Differences distinguish these captures, not commercial vs custom devices. "
                              "No differences does not prove indistinguishability. Serial values and paths "
                              "normally differ between individual commercial units too.",
            "limitations": list(dict.fromkeys(reference.get("limitations", []) + candidate.get("limitations", [])))}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list HID mice and other collections")
    capture = commands.add_parser("snapshot", help="capture one enumerated collection")
    capture.add_argument("--index", type=int, required=True)
    capture.add_argument("--output", type=Path, required=True)
    diff = commands.add_parser("compare", help="compare two snapshots without HID dependencies")
    diff.add_argument("reference", type=Path)
    diff.add_argument("candidate", type=Path)
    diff.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "compare":
            report = compare(json.loads(args.reference.read_text(encoding="utf-8")),
                             json.loads(args.candidate.read_text(encoding="utf-8")))
            rendered = json.dumps(report, indent=2)
            print(rendered)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered + "\n", encoding="utf-8")
            return 1 if report["differences"] else 0
        devices = enumerate_devices()
        if args.command == "list":
            for i, d in enumerate(devices):
                mouse = "mouse" if d.get("usage_page") == 1 and d.get("usage") == 2 else "HID"
                print(f"[{i}] {mouse} {d['vendor_id']:04X}:{d['product_id']:04X} "
                      f"{d.get('manufacturer_string')} / {d.get('product_string')} "
                      f"usage={d.get('usage_page')}:{d.get('usage')} interface={d.get('interface_number')}")
            if not devices:
                print("No HID devices enumerated; check connection and OS permissions.")
            return 0
        if args.index < 0 or args.index >= len(devices):
            raise ValueError("index not present; run list again")
        report = snapshot(devices[args.index])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Saved {args.output}. Descriptor: "
              f"{report['descriptor']['source'] if report['descriptor'] else 'unavailable'}.")
        for limitation in report["limitations"]:
            print(f"  {limitation}")
        return 0
    except ImportError:
        parser.exit(2, "Install HID support: python -m pip install hidapi==0.15.0\n")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"HID check failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
