import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from hid_check import compare, descriptor_summary, usb_descriptor_summary, usb_hub_summary
from replay import Event, Relay, circle_rows, frame, load_rows, plan


class PlanTests(unittest.TestCase):
    def test_circle_closes_without_clicks(self):
        events, shifted = plan(circle_rows())
        self.assertEqual(len(events), 300)
        self.assertEqual(events[-1].due_us, 6_000_000)
        self.assertEqual((sum(e.dx for e in events), sum(e.dy for e in events)), (0, 0))
        self.assertTrue(all(e.buttons == 0 for e in events))
        self.assertEqual(shifted, 0)

    def test_relative_counts_and_click_opt_in(self):
        rows = [[0, 400, 300, 0], [8, 410, 295, 0], [16, 414, 299, 1]]
        events, shifted = plan(rows)
        self.assertEqual(events, [Event(8000, 10, -5), Event(18000, 4, 4)])
        self.assertEqual(shifted, 1)
        clicked, _ = plan(rows, clicks=True)
        self.assertEqual(clicked[-2:], [Event(18000, 4, 4, 1), Event(48000, 0, 0)])

    def test_cumulative_rounding_preserves_fractional_displacement(self):
        events, _ = plan([[i * 8, i, -i, 0] for i in range(11)], counts_per_pixel=0.3)
        self.assertEqual(sum(e.dx for e in events), 3)
        self.assertEqual(sum(e.dy for e in events), -3)

    def test_crowded_reports_and_click_hold_shift_later_motion(self):
        events, shifted = plan([[0, 0, 0, 0], [0.2, 1, 1, 1], [0.3, 2, 2, 0]], clicks=True)
        self.assertEqual([e.due_us for e in events], [200, 30200, 40200])
        self.assertEqual(shifted, 1)

    def test_reject_malformed_or_unsafe_plans(self):
        invalid = [[], [[0, 0, 0, 0]], [[0, 0, 0, 1], [1, 1, 1, 0]],
                   [[0, 0, 0, 0], [-1, 1, 1, 0]], [[0, 0, 0, 0], [1, float('nan'), 0, 0]],
                   [[0, 0, 0, 0], [1, 32768, 0, 0]], [[0, 0, 0, 0], [60001, 0, 0, 0]],
                   [[0, 0, 0, 0], [1, 0, 0, 2]], [[0, 0, 0, 0], [1, 0, 0]],
                   [[0, 0, 0, 0], [2, 0, 0, 0], [1, 0, 0, 0]]]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                plan(rows)

    def test_power_shell_encodings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'path.json'
            rows = [[0, 0, 0, 0], [8, 1, 1, 0]]
            for encoding in ('utf-8-sig', 'utf-16'):
                path.write_text(json.dumps(rows), encoding=encoding)
                self.assertEqual(load_rows(path), rows)

    def test_crc_known_vector(self):
        self.assertEqual(frame('123456789'), b'123456789*29B1\n')

    def test_split_large_motion_preserves_counts_and_click_order(self):
        events, _ = plan([[0, 0, 0, 0], [1, 1000, -600, 1]], clicks=True)
        self.assertEqual((sum(e.dx for e in events), sum(e.dy for e in events)), (1000, -600))
        self.assertTrue(all(max(abs(e.dx), abs(e.dy)) <= 127 for e in events))
        self.assertTrue(all(b.due_us - a.due_us >= 10000 for a, b in zip(events, events[1:])))
        self.assertEqual([e.buttons for e in events], [0] * 7 + [1, 0])

    def test_split_positive_negative_boundaries(self):
        for dx in (-32767, -255, -128, -127, 0, 127, 128, 255, 32767):
            for dy in (-255, -1, 0, 255):
                with self.subTest(dx=dx, dy=dy):
                    events, _ = plan([[0, 0, 0, 0], [1, dx, dy, 0]])
                    self.assertEqual((sum(e.dx for e in events), sum(e.dy for e in events)), (dx, dy))
                    self.assertTrue(all(max(abs(e.dx), abs(e.dy)) <= 127 for e in events))

    def test_split_cannot_overflow_plan_limits(self):
        with self.assertRaisesRegex(ValueError, 'firmware limits'):
            plan([[i * 10, (i % 2) * 32767, 0, 0] for i in range(18)])

    def test_short_click_hold_still_obeys_polling_gap(self):
        events, _ = plan([[0, 0, 0, 0], [1, 1, 0, 1]], clicks=True, click_hold_ms=1)
        self.assertEqual(events[-1].due_us - events[-2].due_us, 10000)


class FakeSerial:
    def __init__(self, fail=None):
        self.lines = []
        self.writes = []
        self.fail = fail

    def reset_input_buffer(self):
        self.lines.clear()

    @property
    def in_waiting(self):
        return bool(self.lines)

    def write(self, data):
        self.writes.append(data)
        command = data.decode().strip().split('*')[0]
        if 'STOP' in command:
            self.lines.append(b'OK STOP\n')
        elif command == 'HELLO':
            self.lines.append(b'OK HELLO 2 4096 10000 60000000 1500 USB=1\n')
        elif command == 'IDENTITY':
            self.lines.append(b'OK IDENTITY VID=046D PID=C077\n')
        elif command.startswith('LOAD '):
            self.lines.append(b'OK LOAD\n')
        elif command.startswith('E '):
            reply = self.fail or f'OK E {command.split()[1]}'
            self.lines.append((reply + '\n').encode())
        elif command == 'RUN':
            self.lines.append(b'OK RUN\n')
        elif command == 'PING':
            self.lines.append(b'DONE 1 200\n')

    def readline(self):
        return self.lines.pop(0) if self.lines else b''


class RelayTests(unittest.TestCase):
    def test_identity_query(self):
        port = FakeSerial()
        self.assertEqual(Relay(port).identity(), 'OK IDENTITY VID=046D PID=C077')
        self.assertEqual(port.writes, [frame('IDENTITY')])

    def test_identity_rejects_unexpected_reply(self):
        with patch.object(Relay, 'read_line', return_value='ERR COMMAND'):
            with self.assertRaisesRegex(RuntimeError, 'unexpected identity response'):
                Relay(FakeSerial()).identity()

    def test_preload_run_completion_and_stop(self):
        port = FakeSerial()
        self.assertEqual(Relay(port).play([Event(8000, 3, 4)]),
                         {'reports': 1, 'max_dispatch_lateness_us': 200})
        commands = [data.decode().strip().split('*')[0] for data in port.writes]
        self.assertLess(commands.index('E 0 8000 3 4 0 0'), commands.index('RUN'))
        self.assertEqual(port.writes[-1], b'\nSTOP\n')

    def test_board_error_aborts_before_run(self):
        port = FakeSerial('ERR CRC')
        with self.assertRaisesRegex(RuntimeError, 'ERR CRC'):
            Relay(port).play([Event(8000, 3, 4)])
        self.assertFalse(any(data.startswith(b'RUN*') for data in port.writes))
        self.assertEqual(port.writes[-1], b'\nSTOP\n')

    def test_lost_ack_sends_stop(self):
        port = FakeSerial()
        with patch.object(Relay, 'read_line', side_effect=TimeoutError), self.assertRaises(TimeoutError):
            Relay(port).play([Event(8000, 3, 4)])
        self.assertEqual(port.writes[-1], b'\nSTOP\n')

    def test_old_protocol_is_rejected_before_upload(self):
        port = FakeSerial()
        with patch.object(Relay, 'probe', return_value='OK HELLO 1 4096 1000 60000000 1500 USB=1'):
            with self.assertRaisesRegex(RuntimeError, 'incompatible firmware'):
                Relay(port).play([Event(10000, 1, 0)])
        self.assertEqual(port.writes, [b'\nSTOP\n'])


class DescriptorTests(unittest.TestCase):
    def test_actual_firmware_report_matches_wire_layout(self):
        source = Path('firmware/esp32_mouse/usb_profile.h').read_text()
        literal = source.split('REPORT_DESCRIPTOR[] = {')[1].split('};')[0]
        data = bytes(int(v, 16) for v in re.findall(r'0x([0-9A-Fa-f]{2})', literal))
        summary = descriptor_summary(data)
        self.assertEqual(summary['reports'], [{'type': 'input', 'report_id': 0,
                                               'payload_bits': 32, 'wire_bytes': 4}])
        xy = next(f for f in summary['fields'] if f['usages'] == [0x30, 0x31, 0x38])
        self.assertTrue(xy['relative'])
        self.assertEqual((xy['bits'], xy['logical_min'], xy['logical_max']), (8, -127, 127))
        self.assertEqual(summary['collections'][0]['usages'], [2])
        buttons = next(f for f in summary['fields'] if f['usage_page'] == 9 and not f['constant'])
        self.assertEqual((buttons['usage_min'], buttons['usage_max'], buttons['count']), (1, 3, 3))

    def test_firmware_configuration_has_only_boot_mouse_input(self):
        source = Path('firmware/esp32_mouse/usb_profile.h').read_text()
        literal = source.split('CONFIG_DESCRIPTOR[] = {')[1].split('};')[0]
        data = bytes(int(v, 16) for v in re.findall(r'0x([0-9A-Fa-f]{2})', literal))
        parsed = usb_descriptor_summary(data)
        self.assertEqual(int.from_bytes(data[2:4], 'little'), len(data))
        self.assertEqual(parsed['interfaces'], [{'number': 0, 'alternate': 0, 'endpoints': 1,
                                                'class': 3, 'subclass': 1, 'protocol': 2}])
        self.assertEqual(parsed['endpoints'], [{'interface': 0, 'address': 0x81,
                                               'attributes': 3, 'max_packet': 4, 'interval': 10}])
        self.assertEqual((data[6], data[17]), (0, 0))  # no config/interface strings
        self.assertIn('.iSerialNumber = 0', source)
        self.assertIn('.bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE', source)

    def test_global_push_pop(self):
        summary = descriptor_summary(bytes.fromhex('750895018102A4751095028102B48102'))
        self.assertEqual([f['bits'] for f in summary['fields']], [8, 16, 8])
        self.assertEqual(summary['reports'][0]['payload_bits'], 48)

    def test_truncation_is_not_treated_as_valid(self):
        for raw in ('26ff', 'fe020001', 'b4'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                descriptor_summary(bytes.fromhex(raw))

    def test_linux_usb_endpoints(self):
        raw = bytes.fromhex('12010002000000403a300200000101020301'
                            '090222000101008032' '090400000103000000'
                            '07058103400001')
        parsed = usb_descriptor_summary(raw)
        self.assertEqual(parsed['endpoints'][0]['interval'], 1)
        self.assertEqual(parsed['configurations'][0]['max_power_ma'], 100)

    def test_hub_summary_exposes_the_reported_tinyusb_fingerprint(self):
        capture = json.loads(Path('reports/2026-10-10-hid-investigation/usb_probe.json').read_text())
        calls = []
        def get_string(kind, index, language):
            calls.append(index)
            self.assertEqual((kind, language), (3, 0x0409))
            encoded = capture['strings'][str(index)].encode('utf-16-le')
            return bytes([2 + len(encoded), 3]) + encoded
        summary = usb_hub_summary(bytes.fromhex(capture['device_descriptor_hex']),
                                  bytes.fromhex(capture['config_hex']), capture['speed_code'], get_string)
        self.assertEqual(summary['strings']['configuration'], 'TinyUSB Device')
        self.assertEqual(summary['strings']['interface_0_0'], 'TinyUSB HID')
        self.assertEqual(summary['speed_mbps'], '12')
        self.assertTrue(summary['device']['serial_present'])
        self.assertNotIn(3, calls)  # exclude individual serial values from fingerprints

    def test_hub_summary_new_profile_has_no_extra_strings(self):
        source = Path('firmware/esp32_mouse/usb_profile.h').read_text()
        literal = source.split('CONFIG_DESCRIPTOR[] = {')[1].split('};')[0]
        config = bytes(int(v, 16) for v in re.findall(r'0x([0-9A-Fa-f]{2})', literal))
        device = bytes.fromhex('12010002000000406d0477c0007201020001')
        calls = []
        def get_string(kind, index, language):
            calls.append(index)
            encoded = {1: 'Logitech', 2: 'USB Optical Mouse'}[index].encode('utf-16-le')
            return bytes([2 + len(encoded), 3]) + encoded
        summary = usb_hub_summary(device, config, 1, get_string)
        self.assertEqual(calls, [1, 2])
        self.assertFalse(summary['device']['serial_present'])
        self.assertIsNone(summary['strings']['configuration'])
        self.assertIsNone(summary['strings']['interface_0_0'])

    def test_hub_summary_rejects_bad_string(self):
        with self.assertRaisesRegex(ValueError, 'string descriptor'):
            usb_hub_summary(bytes.fromhex('12010002000000406d0477c0007201020001'),
                            bytes.fromhex('090209000001008032'), 1,
                            lambda *args: b'\x06\x03a\x00')


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.reference = {'schema': 1, 'host': {'system': 'Windows'},
                          'identity': {'vendor_id': 0x1234, 'product_id': 1},
                          'serial_number': 'unit-a', 'path': 'port-one',
                          'descriptor': None, 'topology': None, 'limitations': []}

    def test_identical_missing_data_is_inconclusive(self):
        result = compare(self.reference, self.reference)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertTrue(result['unknown'])

    def test_vid_difference_is_observable(self):
        candidate = copy.deepcopy(self.reference)
        candidate['identity']['vendor_id'] = 0x303A
        result = compare(self.reference, candidate)
        self.assertEqual(result['verdict'], 'observable_differences')
        self.assertEqual(result['differences'][0]['field'], 'identity.vendor_id')

    def test_new_hub_evidence_is_unknown_in_old_snapshot_not_a_difference(self):
        self.reference['topology'] = {'pnp_nodes': []}
        candidate = copy.deepcopy(self.reference)
        candidate['topology']['usb'] = {'device': {'serial_present': False}}
        result = compare(self.reference, candidate)
        self.assertEqual(result['differences'], [])
        self.assertIn('topology.usb', result['unknown'])

    def test_unit_serial_and_host_path_are_not_authenticity_evidence(self):
        candidate = copy.deepcopy(self.reference)
        candidate.update(serial_number='unit-b', path='port-two')
        result = compare(self.reference, candidate)
        self.assertTrue(result['unit_serials_differ'])
        self.assertEqual(result['differences'], [])

    def test_raw_and_reconstructed_descriptors_are_not_compared(self):
        candidate = copy.deepcopy(self.reference)
        self.reference['descriptor'] = {'source': 'windows-reconstructed', 'sha256': 'a'}
        candidate['descriptor'] = {'source': 'linux-sysfs-raw', 'sha256': 'b'}
        result = compare(self.reference, candidate)
        self.assertEqual(result['differences'], [])
        self.assertTrue(any('acquisition' in s for s in result['unknown']))

    def test_complete_equal_fingerprints_still_not_proof(self):
        self.reference['identity'] = {key: 1 for key in __import__('hid_check').IDENTITY_KEYS}
        self.reference['descriptor'] = {'source': 'hidapi-raw', 'sha256': 'a', 'summary': {}}
        self.reference['topology'] = {}
        result = compare(self.reference, self.reference)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['unknown'], [])


if __name__ == '__main__':
    unittest.main()
