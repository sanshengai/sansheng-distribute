"""X video wait: keep waiting while upload receipts grow; time out only on no progress (S4E02 2026-11-02)."""
import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import x_compose_ready as xc
from publish_core import PublishError


class Page:
    def __init__(self,ready_after):self.calls=0;self.ready_after=ready_after
    def wait_for_function(self,*a,**k):
        self.calls+=1
        if self.calls<self.ready_after:raise TimeoutError()


class ProgressWaitTests(unittest.TestCase):
    def setUp(self):patch.object(xc,'realize',lambda p:None).start()
    def tearDown(self):patch.stopall()

    def test_slow_but_progressing_upload_outlives_fixed_limit(self):
        # receipts arrive every 3 slices; fixed timeout would be 2 slices
        p=Page(ready_after=20);n={'v':0}
        def prog():n['v']+=1;return n['v']//3
        log=[]
        xc.wait_painted(p,'x',timeout_ms=20,slice_ms=10,progress=prog,idle_ms=50,cap_ms=10000,record=log.append)
        self.assertEqual(p.calls,20);self.assertTrue(all(r['idle_ms']<50 for r in log))

    def test_no_progress_times_out_on_idle(self):
        p=Page(ready_after=10**6)
        with self.assertRaises(PublishError) as cm:
            xc.wait_painted(p,'x',slice_ms=10,progress=lambda:5,idle_ms=40,cap_ms=10000)
        self.assertIn('无新进展',str(cm.exception));self.assertEqual(p.calls,4)

    def test_total_cap_even_with_progress(self):
        p=Page(ready_after=10**6);n={'v':0}
        def prog():n['v']+=1;return n['v']
        with self.assertRaises(PublishError):
            xc.wait_painted(p,'x',slice_ms=10,progress=prog,idle_ms=40,cap_ms=100)
        self.assertEqual(p.calls,10)

    def test_without_progress_keeps_fixed_limit(self):
        p=Page(ready_after=10**6)
        with self.assertRaises(PublishError):xc.wait_painted(p,'x',timeout_ms=30,slice_ms=10)
        self.assertEqual(p.calls,3)


if __name__=='__main__':unittest.main()
