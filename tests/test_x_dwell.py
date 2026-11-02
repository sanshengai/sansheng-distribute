import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import x_publish_browser as xb
from publish_core import PublishError,read_json


class Page:
    def __init__(self):self.waited=0
    def wait_for_timeout(self,ms):self.waited+=ms


class XDwellTests(unittest.TestCase):
    def test_unset_means_no_pause(self):
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('SANSHENG_X_DWELL',None)
            self.assertIsNone(xb.dwell(Page(),{},Path('/nonexistent')))

    def test_bounds_validated(self):
        for bad in ('x','10','5,700','9,3'):
            with self.subTest(bad=bad),patch.dict(os.environ,{'SANSHENG_X_DWELL':bad}),self.assertRaises(PublishError):
                xb.dwell_bounds()

    def test_pauses_once_and_records(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'SANSHENG_X_DWELL':'0,0'}):
            marker=Path(d)/'m.json';saved={};page=Page()
            self.assertEqual(xb.dwell(page,saved,marker),0)
            self.assertEqual(read_json(marker)['dwell_seconds'],0)
            self.assertIsNone(xb.dwell(page,saved,marker))  # never twice


if __name__=='__main__':unittest.main()
