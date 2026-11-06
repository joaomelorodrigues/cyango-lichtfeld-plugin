"""Static checks that the RML, the RCSS and the panel code agree. LichtFeld is not needed."""

from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RML = ROOT / "panels" / "main_panel.rml"
RCSS = ROOT / "panels" / "main_panel.rcss"
PANEL = ROOT / "panels" / "main_panel.py"
LOGO = ROOT / "panels" / "cyango-logo.tga"


class PanelContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ET.parse(RML).getroot()
        cls.rml = RML.read_text(encoding="utf-8")
        cls.panel = PANEL.read_text(encoding="utf-8")

    def test_rml_is_well_formed_and_uses_the_cyango_model(self) -> None:
        body = self.root.find("body")
        self.assertEqual(body.attrib.get("data-model"), "cyango")
        self.assertEqual(self.root.find("./head/link").attrib["href"], "main_panel.rcss")
        self.assertIn('MODEL = "cyango"', self.panel)
        views = [child.attrib.get("data-if") for child in body if child.attrib.get("data-if")]
        self.assertEqual(views, ["show_form", "show_progress", "show_success"])

    def test_every_event_and_value_in_the_rml_is_bound(self) -> None:
        events = set(re.findall(r'data-event-[a-z]+="([a-z_]+)"', self.rml))
        values = set(re.findall(r'data-value="([a-z_]+)"', self.rml))
        conditions = {
            name.lstrip("!") for name in re.findall(r'data-(?:if|attrif-[a-z]+|class-[a-z]+)="([!a-z_]+)"', self.rml)
        }
        mustaches = set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", self.rml))
        for name in events | values | conditions | mustaches:
            self.assertRegex(self.panel, rf'"{name}"', f"{name} is used in the RML but not bound in the panel")

    def test_select_options_match_the_panel_constants(self) -> None:
        formats = re.findall(r'<option value="(spz|ply|sog)"', self.rml)
        self.assertEqual(tuple(formats), ("spz", "ply", "sog"))
        self.assertIn('FORMATS = ("spz", "ply", "sog")', self.panel)
        self.assertIn('data-for="folder : folders"', self.rml)
        self.assertIn('bind_record_list("folders")', self.panel)

    def test_every_class_has_a_style_and_the_logo_is_an_uncompressed_tga(self) -> None:
        css = RCSS.read_text(encoding="utf-8")
        used = {c for value in re.findall(r'class="([^"]+)"', self.rml) for c in value.split() if c.startswith("cy-")}
        missing = sorted(c for c in used if f".{c}" not in css and c != "cy-topbar-copy")
        self.assertEqual(missing, [])
        header = LOGO.read_bytes()[:18]
        self.assertEqual(header[2], 2, "type 2: uncompressed true color")
        self.assertEqual(header[16], 32)
        self.assertIn('src="cyango-logo.tga"', self.rml)


if __name__ == "__main__":
    unittest.main()
