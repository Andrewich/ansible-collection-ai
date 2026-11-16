"""Tests for the native redfish_identity filter (no external tools)."""
from __future__ import print_function

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "plugins", "filter"))

import redfish_identity as R  # noqa: E402

HUAWEI = ("Manufacturer = Huawei\n"
          "Model = 2288X V5\n"
          "Processors.@odata.id.Model = Intel(R) Xeon(R) Gold 5220R CPU @ 2.20GHz")

FUJITSU = ("Manufacturer = FUJITSU\n"
           "Model = PRIMERGY RX2530 M5\n"
           "Processors.@odata.id.Members.@odata.id.Model = "
           "Intel(R) Xeon(R) Gold 6240R CPU @ 2.40GHz")


def payload(name):
    with open(os.path.join(ROOT, "fixtures", name), encoding="utf-8") as handle:
        return handle.read()


class RealPayloadTest(unittest.TestCase):
    def test_huawei(self):
        self.assertEqual(R.redfish_identity(payload("real_huawei.json")), HUAWEI)

    def test_fujitsu(self):
        self.assertEqual(R.redfish_identity(payload("real_fujitsu.json")), FUJITSU)

    def test_accepts_bytes(self):
        raw = payload("real_huawei.json").encode("utf-8")
        self.assertEqual(R.redfish_identity(raw), HUAWEI)


class ReduceTest(unittest.TestCase):
    def test_mainboard_and_cpu_vendor_are_dropped(self):
        text = (
            '{\n'
            '  "Oem": {\n'
            '    "MainBoard": {\n'
            '      "Manufacturer": "MiTAC",\n'
            '      "Model": "D3383"\n'
            '    }\n'
            '  },\n'
            '  "Manufacturer": "FUJITSU",\n'
            '  "Model": "PRIMERGY RX2530 M5",\n'
            '  "Processors": {\n'
            '    "Members": [\n'
            '      {\n'
            '        "Manufacturer": "Intel(R) Corporation",\n'
            '        "Model": "Intel(R) Xeon(R) Gold 6240R CPU @ 2.40GHz"\n'
            '      }\n'
            '    ]\n'
            '  }\n'
            '}'
        )
        result = R.redfish_identity(text)
        self.assertIn("Manufacturer = FUJITSU", result)
        self.assertIn("Model = PRIMERGY RX2530 M5", result)
        self.assertIn("Processors.Members.Model = Intel(R) Xeon(R) Gold 6240R", result)
        self.assertNotIn("MiTAC", result)
        self.assertNotIn("D3383", result)
        self.assertNotIn("Intel(R) Corporation", result)

    def test_duplicate_lines_are_collapsed(self):
        text = (
            '{\n'
            '  "Manufacturer": "Huawei",\n'
            '  "Model": "2288X V5",\n'
            '  "Processors": {\n'
            '    "Members": [\n'
            '      {\n'
            '        "Model": "Xeon Gold 5220R"\n'
            '      },\n'
            '      {\n'
            '        "Model": "Xeon Gold 5220R"\n'
            '      }\n'
            '    ]\n'
            '  }\n'
            '}'
        )
        result = R.redfish_identity(text)
        self.assertEqual(result.count("Xeon Gold 5220R"), 1)

    def test_malformed_json_is_still_processed(self):
        text = (
            '{\n'
            '  "Manufacturer": "Huawei",\n'
            '  "Processors": {\n'
            '    "@odata.id": "/redfish/v1/Systems/1/Processors",\n'
            '    [\n'
            '      {\n'
            '        "Model": "Intel(R) Xeon(R) Gold 5220R CPU @ 2.20GHz"\n'
            '      }\n'
            '    ]\n'
            '  }\n'
            '}'
        )
        self.assertIn("Processors.@odata.id.Model = Intel(R) Xeon(R) Gold 5220R", 
                      R.redfish_identity(text))

    def test_empty_input(self):
        self.assertEqual(R.redfish_identity(""), "")


if __name__ == "__main__":
    unittest.main()
