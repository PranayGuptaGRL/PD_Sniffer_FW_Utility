from __future__ import annotations

import unittest

from usb_pd_decoder import windows_driver


class WindowsDriverTest(unittest.TestCase):
    def test_grl_driver_package_metadata(self) -> None:
        package = windows_driver.get_grl_driver_package()

        self.assertEqual(package.vid, 0x227F)
        self.assertEqual(package.pid, 0x0005)
        self.assertIn("USB\\VID_227F&PID_0005", package.hardware_ids)
        self.assertIn("USB\\VID_227F&PID_0005&MI_00", package.hardware_ids)
        self.assertEqual(package.inf_path.name, "grl_sniffer_winusb.inf")
        self.assertEqual(package.cat_path.name, "grl_sniffer_winusb.cat")
        self.assertTrue(package.has_inf)

    def test_grl_inf_contains_expected_ids(self) -> None:
        package = windows_driver.get_grl_driver_package()
        text = package.inf_path.read_text(encoding="utf-8")

        for hardware_id in package.hardware_ids:
            self.assertIn(hardware_id, text)
        self.assertIn(windows_driver.GRL_DEVICE_INTERFACE_GUID, text)


if __name__ == "__main__":
    unittest.main()
