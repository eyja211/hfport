# -*- coding: utf-8 -*-
import unittest

from hfport import porter as P


class PortPlanTests(unittest.TestCase):
    def test_source_namespace_is_short_ascii(self):
        self.assertEqual(
            P.derive_namespace(r"C:\mods\Promised Mystery of Moisture V-1.9.6.exe"),
            "pmm",
        )
        self.assertEqual(
            P.derive_namespace(r"C:\mods\HF Eyja 0.1.7.exe"),
            "eyja",
        )

    def test_namespace_increments_when_target_role_id_is_occupied(self):
        got = P.allocate_namespace(
            "pmm",
            selected_ids=["lucas", "drew"],
            occupied_ids={"lucas", "drew", "lucas_pmm"},
            occupied_classes=set(),
        )

        self.assertEqual(got, "pmm2")

    def test_explicit_namespace_is_sanitized(self):
        self.assertEqual(P.sanitize_namespace(" My Mod! 2 "), "mymod2")

    def test_namespace_increments_when_lmi_prefix_is_occupied(self):
        got = P.allocate_namespace(
            "pmm",
            selected_ids=["lucas"],
            occupied_ids={"lucas"},
            occupied_classes={"Data.Global_pmm_sharedLmi"},
        )

        self.assertEqual(got, "pmm2")

    def test_namespace_increments_when_resource_suffix_is_occupied(self):
        got = P.allocate_namespace(
            "pmm",
            selected_ids=["lucas"],
            occupied_ids={"lucas"},
            occupied_classes=set(),
            occupied_names={"lucasB@pmm"},
        )

        self.assertEqual(got, "pmm2")


if __name__ == "__main__":
    unittest.main()
