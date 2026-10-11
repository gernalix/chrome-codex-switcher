import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest
from chrome_codex_switcher import nv
from chrome_codex_switcher.daemon import search_dashboard_html

class NVTests(unittest.TestCase):
    def test_single_source_and_dashboard(self):
        with tempfile.TemporaryDirectory() as td, patch.object(nv, 'ROOT', Path(td)):
            p=Path(td)/'NV.txt'
            self.assertEqual(nv.release_label(),'NV unavailable')
            p.write_bytes(b'2\n')
            self.assertEqual(nv.release_label(),'NV 2')
            self.assertIn('id="project-nv"',search_dashboard_html())
            self.assertIn('>NV 2</small>',search_dashboard_html())
            for raw in (b'0\n',b'02\n',b'2',b'2\r\n'):
                p.write_bytes(raw)
                self.assertEqual(nv.release_label(),'NV unavailable')
            p.write_bytes(b'2\n');(Path(td)/'version.txt').write_text('2')
            self.assertEqual(nv.release_label(),'NV unavailable')
