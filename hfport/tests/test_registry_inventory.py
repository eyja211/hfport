# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from unittest import mock

from hfport import registry as R


GLOBAL_TEXT = r'''
public static var charListOrder:Array = ["lucas","drew"];
public static var pow:Array = [];
pow["lucas"] = {"i":1,"hp0":500,"state":"a"};
pow["drew"] = {"i":2,"hp0":550,"state":"a"};
langWords["lucas"] = {"en":"Lucas","b5":"Lucas"};
langWords["drew"] = {"en":"Drew","b5":"Drew"};
pow["lucas"].c = c1;
pow["drew"].c = c2;
pow["lucas_pm"] = {"i":3,"hp0":600,"state":"a"};
charListOrder.push("lucas_pm");
langWords["lucas_pm"] = {"en":"Little hero (PM)","b5":"Little hero (PM)"};
pow["lucas_pm"].c = pow["drew"].c;
Console.sfx[87] = new Sfx(PortSnd21,5);
'''

SELECT_TEXT = r'''
private static var charOption:Array = [{
  "value":-1,
  "name":"[Random]",
  "id":"random"
},{
  "value":0,
  "name":"Lucas",
  "id":"lucas"
},{
  "value":1,
  "name":"Drew",
  "id":"drew"
}];
charOption.push({
  "value":2,
  "name":"Little hero (PM)",
  "id":"lucas_pm"
});
'''


class RegistryInventoryTests(unittest.TestCase):
    def test_inventory_includes_tail_appends(self):
        inv = R.build_inventory(GLOBAL_TEXT, SELECT_TEXT)

        self.assertEqual(
            [entry[2] for entry in inv.char_options],
            ["random", "lucas", "drew", "lucas_pm"],
        )
        self.assertEqual(inv.char_list_order, ["lucas", "drew", "lucas_pm"])
        self.assertIn("lucas_pm", inv.pow)

    def test_binding_chain_resolves_to_login_field(self):
        inv = R.build_inventory(GLOBAL_TEXT, SELECT_TEXT)

        self.assertEqual(inv.binding_codes["lucas"], 1)
        self.assertEqual(inv.binding_codes["drew"], 2)
        self.assertEqual(inv.binding_codes["lucas_pm"], 2)

    def test_inventory_reads_appended_sound_registration(self):
        inv = R.build_inventory(GLOBAL_TEXT, SELECT_TEXT)

        self.assertEqual(inv.sound_classes[87], "PortSnd21")

    def test_binding_target_falls_back_to_target_same_role(self):
        target = R.choose_binding_target(
            source_slot="jenny",
            source_code=4,
            target_codes={"lucas": 1, "jenny": 8, "eason": 8},
        )

        self.assertEqual(target, "jenny")

    def test_export_cache_key_changes_with_input_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, "same-name-a.exe")
            b = os.path.join(td, "same-name-b.exe")
            with open(a, "wb") as f:
                f.write(b"first")
            with open(b, "wb") as f:
                f.write(b"second")

            self.assertNotEqual(
                R.export_cache_key(a),
                R.export_cache_key(b),
            )

    def test_load_inventory_uses_hash_scoped_export_directory(self):
        with tempfile.TemporaryDirectory() as td:
            exe = os.path.join(td, "source.exe")
            with open(exe, "wb") as f:
                f.write(b"source-build")
            global_as = os.path.join(td, "Global.as")
            select_as = os.path.join(td, "SelectCharPanel.as")
            with open(global_as, "w", encoding="utf-8") as f:
                f.write(GLOBAL_TEXT)
            with open(select_as, "w", encoding="utf-8") as f:
                f.write(SELECT_TEXT)

            with mock.patch.object(
                R,
                "ensure_exports",
                return_value={
                    "Data.Global": global_as,
                    "Web_misc.SelectCharPanel": select_as,
                },
            ) as ensure:
                inv = R.load_inventory(td, "ffdec.jar", exe)

            out_dir = ensure.call_args[0][3]
            self.assertIn(R.export_cache_key(exe), out_dir)
            self.assertIn("lucas_pm", inv.pow)


if __name__ == "__main__":
    unittest.main()
