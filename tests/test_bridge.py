import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ht3_bridge", ROOT / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class PacketTests(unittest.TestCase):
    def test_get_and_set_packets(self):
        self.assertEqual(bridge.packet(2), bytes.fromhex("00020000"))
        self.assertEqual(bridge.packet(4, [1]), bytes.fromhex("1004010101"))

    def test_parser_recovers_fragmented_frames_and_skips_vendor_noise(self):
        parser = bridge.Frames()
        self.assertEqual(parser.feed(bytes.fromhex("ff990002")), [])
        self.assertEqual(parser.feed(bytes.fromhex("0159")), [])
        self.assertEqual(parser.feed(bytes.fromhex("590030010202")), [(2, b"\x59"), (48, b"\x02")])

    def test_parser_rejects_a_bad_checksum_and_recovers(self):
        parser = bridge.Frames()
        stream = bytes.fromhex("00020159000013010101")
        self.assertEqual(parser.feed(stream), [(19, b"\x01")])


class DecodeTests(unittest.TestCase):
    def test_decodes_supported_state(self):
        self.assertEqual(bridge.decode(2, b"\x59"), {"battery": 89})
        self.assertEqual(bridge.decode(48, b"\x02"), {"mode": "transparency"})
        self.assertEqual(bridge.decode(19, b"\x00"), {"spatial": False})
        gains = [-50, -1, 0, 1, 50, 10, 20, 30, 40, -20]
        payload = bytes([value & 255 for value in gains] + bridge.FREQUENCIES)
        self.assertEqual(bridge.decode(11, payload), {"eq": gains})

    def test_rejects_invalid_lengths_values_and_frequency_table(self):
        with self.assertRaises(ValueError):
            bridge.decode(2, b"")
        with self.assertRaises(ValueError):
            bridge.decode(2, b"\x65")
        with self.assertRaises(ValueError):
            bridge.decode(48, b"\x05")
        with self.assertRaises(ValueError):
            bridge.decode(11, bytes(20))


class DeviceDiscoveryTests(unittest.TestCase):
    @mock.patch.object(bridge.subprocess, "run")
    def test_lists_only_paired_tozo_ht3_devices(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Device AA:BB:CC:DD:EE:FF TOZO HT3\n"
                "Device 11:22:33:44:55:66 Other headset\n"
                "\x1b[0mDevice 77:88:99:AA:BB:CC tozo ht3 office\x1b[0m\n"
            ),
            stderr="",
        )
        self.assertEqual(
            bridge.devices(),
            [
                {"address": "AA:BB:CC:DD:EE:FF", "name": "TOZO HT3"},
                {"address": "77:88:99:AA:BB:CC", "name": "tozo ht3 office"},
            ],
        )
        run.assert_called_once_with(
            ["bluetoothctl", "devices", "Paired"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.headphones = bridge.Headphones()
        self.headphones.sock = mock.Mock()
        self.headphones.state = {"connected": True}

    def test_mode_requires_confirming_readback(self):
        self.headphones.query = mock.Mock(side_effect=[{"mode": "normal"}, {"mode": "anc"}])
        with mock.patch.object(bridge.time, "sleep"):
            self.headphones.set_mode("anc")
        self.headphones.sock.sendall.assert_called_once_with(bridge.packet(4, [1]))

    def test_mode_rejects_unknown_and_unconfirmed_values(self):
        with self.assertRaises(ValueError):
            self.headphones.set_mode("gaming")
        self.headphones.query = mock.Mock(side_effect=[{"mode": "normal"}, {"mode": "normal"}])
        with mock.patch.object(bridge.time, "sleep"), self.assertRaises(ValueError):
            self.headphones.set_mode("anc")

    def test_eq_checks_spatial_audio_and_confirms_complete_curve(self):
        gains = [-50, -10, 0, 10, 50, 1, 2, 3, 4, 5]
        self.headphones.query = mock.Mock(side_effect=[{"spatial": False}, {"eq": [0] * 10}, {"eq": gains}])
        with mock.patch.object(bridge.time, "sleep"):
            self.headphones.set_eq(gains)
        expected = bridge.packet(11, [value & 255 for value in gains] + bridge.FREQUENCIES + [1])
        self.headphones.sock.sendall.assert_called_once_with(expected)

    def test_eq_rejects_invalid_input_spatial_audio_and_failed_readback(self):
        for value in (None, [0] * 9, [0] * 9 + [51], [0] * 9 + [1.5]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.headphones.set_eq(value)

        self.headphones.query = mock.Mock(return_value={"spatial": True})
        with self.assertRaises(ValueError):
            self.headphones.set_eq([0] * 10)

        self.headphones.query = mock.Mock(side_effect=[{"spatial": False}, {"eq": [0] * 10}, {"eq": [1] * 10}])
        with mock.patch.object(bridge.time, "sleep"), self.assertRaises(ValueError):
            self.headphones.set_eq([0] * 10)

    def test_refresh_clears_stale_capabilities_when_reads_fail(self):
        self.headphones.state.update({"battery": 80, "mode": "anc", "spatial": False, "eq": [0] * 10})
        self.headphones.query = mock.Mock(side_effect=TimeoutError)
        self.headphones.refresh()
        self.assertEqual(self.headphones.state, {"connected": True})


class ManifestTests(unittest.TestCase):
    def test_manifest_entry_point_and_identity_are_stable(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "patrickfanella.ht3")
        self.assertEqual(manifest["entryPoints"], {"barWidget": "Widget.qml"})
        self.assertEqual(manifest["kinds"], ["bar-widget"])
        self.assertTrue((ROOT / manifest["entryPoints"]["barWidget"]).is_file())


if __name__ == "__main__":
    unittest.main()
