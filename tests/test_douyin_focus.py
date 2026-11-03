import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import douyin_focus as f
from publish_core import PublishError
class S:
    def __init__(s):s.sent=[];s.detached=False
    def send(s,m,p=None):s.sent.append(m)
    def detach(s):s.detached=True
class C:
    def __init__(s):s.s=S()
    def new_cdp_session(s,p):return s.s
class P:
    def __init__(s,state):s.context=C();s.state=state;s.waits=[]
    def evaluate(s,js):return s.state(s) if callable(s.state) else s.state
    def wait_for_timeout(s,ms):s.waits.append(ms)
class FocusTests(unittest.TestCase):
    def setUp(self):f.ENABLED=True;f._HELD.clear()
    def test_holds_session_without_detach_and_passes_when_visible(self):
        p=P(dict(vis='visible',focus=True));f.ensure(p);f.ensure(p)
        self.assertEqual(p.context.s.sent.count('Emulation.setFocusEmulationEnabled'),1)
        self.assertFalse(p.context.s.detached)
    def test_hidden_or_unfocused_stops_before_action(self):
        for st in (dict(vis='hidden',focus=True),dict(vis='visible',focus=False),None):
            f._HELD.clear()
            with self.subTest(st=st),self.assertRaises(PublishError):f.ensure(P(st))
    def test_never_activates_os_window(self):
        src=Path(f.__file__).read_text()
        for bad in ('bring_to_front','Page.bringToFront','activateTarget'):self.assertNotIn(bad,src)
class FocusRetryTests(unittest.TestCase):
    def setUp(self):f.ENABLED=True;f._HELD.clear()
    def test_stale_session_is_recreated_then_passes(self):
        sessions=[]
        class C2:
            def new_cdp_session(s,p):x=S();sessions.append(x);return x
        n=[0]
        def st(p):
            n[0]+=1;return dict(vis='visible',focus=True) if n[0]>=3 else dict(vis='hidden',focus=False)
        p=P(st);p.context=C2();rec=[]
        self.assertEqual(f.ensure(p,record=rec.append)['vis'],'visible')
        self.assertEqual(len(sessions),3);self.assertTrue(sessions[0].detached and sessions[1].detached)
        self.assertFalse(sessions[2].detached);self.assertEqual(len(p.waits),2);self.assertEqual(len(rec[-1]),3)
    def test_bounded_and_reports_last_state(self):
        p=P(dict(vis='hidden',focus=False))
        with self.assertRaises(PublishError) as cm:f.ensure(p,attempts=3)
        self.assertIn('重试 3 次',str(cm.exception));self.assertEqual(len(p.waits),2);self.assertNotIn(id(p),f._HELD)
    def test_evaluate_error_is_retried(self):
        n=[0]
        def st(p):
            n[0]+=1
            if n[0]==1:raise RuntimeError('target swapped')
            return dict(vis='visible',focus=True)
        self.assertTrue(f.ensure(P(st))['focus'])
class CaretTests(unittest.TestCase):
    def test_caret_restored_before_topic_button(self):
        src=(Path(f.__file__).parent/'douyin_browser.py').read_text()
        i=src.index("progress('topic_'+str(index+1)+'_search')")
        seg=src[i:src.index("#添加话题",i)]
        self.assertIn('douyin_focus.ensure(page)',seg);self.assertIn('PLACE_CARET_END',seg)
if __name__=='__main__':unittest.main()

class WriteTextTests(unittest.TestCase):
    def test_multiline_uses_native_enter_not_fill_newline(self):
        import douyin_browser as h
        calls=[]
        class F:
            def fill(s,v):calls.append(('fill',v))
            def focus(s,**k):calls.append(('focus',))
            def evaluate(s,js):return True
            def press(s,k,**kw):calls.append(('press',k))
            def press_sequentially(s,v,**kw):calls.append(('type',v))
        h.write_text(F(),'一\n二\n\n三')
        self.assertEqual(calls[0],('fill','一'))
        self.assertNotIn('\n',''.join(c[1] for c in calls if c[0] in('fill','type')))
        self.assertEqual([c for c in calls if c[0]=='press'],[('press','Enter')]*3)
    def test_fill_path_never_receives_newline(self):
        src=(Path(f.__file__).parent/'douyin_browser.py').read_text()
        self.assertNotIn('field.fill(body)',src);self.assertNotIn("field.fill(pack['fields']['composed_text'])",src)

class PublicDescTests(unittest.TestCase):
    def test_only_blank_line_before_topics_may_become_space_newline(self):
        import douyin_publish_browser as s
        d='T 一\n二\n\n#话题 #b'
        self.assertEqual(s.public_desc_forms(d),{d,'T 一\n二 \n#话题 #b'})
        self.assertNotIn('T 一\n*二\n\n#话题 #b',s.public_desc_forms(d))
        self.assertEqual(s.public_desc_forms('T 一'),{'T 一'})

class PublishedCoverCopyTests(unittest.TestCase):
    def test_platform_copy_needs_matching_dims(self):
        import douyin_cover as c
        obj=dict(host='p0-creator-media-private.douyin.com',uri='tos-cn-i-abc/x1');rec=dict(width=1078,height=1440)
        same=dict(covers=dict(vertical='tos-cn-i-abc/x1'))
        self.assertTrue(c.published_matches(same,'portrait',obj,rec))
        copy=dict(covers=dict(vertical='tos-cn-i-dy/y2'),cover_dims=dict(vertical=dict(width=1080,height=1440)))
        self.assertTrue(c.published_matches(copy,'portrait',obj,rec))
        for d in (dict(covers=dict(vertical='tos-cn-i-dy/y2')),
                  dict(covers=dict(vertical='tos-cn-i-dy/y2'),cover_dims=dict(vertical=dict(width=1440,height=1080))),
                  dict(covers=dict(vertical='tos-cn-i-zz/y2'),cover_dims=dict(vertical=dict(width=1078,height=1440)))):
            self.assertFalse(c.published_matches(d,'portrait',obj,rec))


class DwellTests(unittest.TestCase):
    def setUp(self):
        import os;self.os=os;self.old=os.environ.pop('SANSHENG_DOUYIN_DWELL',None)
    def tearDown(self):
        self.os.environ.pop('SANSHENG_DOUYIN_DWELL',None)
        if self.old is not None:self.os.environ['SANSHENG_DOUYIN_DWELL']=self.old
    def test_bounds(self):
        import douyin_publish_browser as s
        self.assertIsNone(s.dwell_bounds())
        self.os.environ['SANSHENG_DOUYIN_DWELL']='30,180';self.assertEqual(s.dwell_bounds(),(30,180))
        for bad in ('x','90,30','0,900'):
            self.os.environ['SANSHENG_DOUYIN_DWELL']=bad
            with self.assertRaises(s.PublishError):s.dwell_bounds()
    def test_dwell_waits_once_and_checkpoints(self):
        import douyin_publish_browser as s
        self.os.environ['SANSHENG_DOUYIN_DWELL']='0,0'
        waits=[];marks=[]
        class P:
            def wait_for_timeout(self,ms):waits.append(ms)
        saved={}
        orig=s.checkpoint;s.checkpoint=lambda sv,m,**kw:(sv.update(kw),marks.append(kw))
        try:
            s.dwell(P(),saved,None);s.dwell(P(),saved,None)
        finally:s.checkpoint=orig
        self.assertEqual(saved['dwell_seconds'],0);self.assertEqual(len(marks),1)
    def test_submit_calls_dwell_before_ready(self):
        import douyin_publish_browser as s,inspect
        src=inspect.getsource(s.submit_once)
        self.assertLess(src.index('dwell(page,saved,marker)'),src.index('options=ready('))


class CoverSavedWaitTests(unittest.TestCase):
    def test_readback_waits_for_changed_src(self):
        import douyin_cover as c
        src=Path(c.__file__).read_text()
        self.assertIn("!old.includes(im.currentSrc)",src)
        self.assertIn("arg=[ROLES[role][0],originals[role]]",src)
        self.assertIn("抖音封面保存后预览未更新",src)


class PlayerReadyFocusTests(unittest.TestCase):
    def test_focus_before_upload_and_rechecked_while_waiting(self):
        import douyin_browser as b,inspect
        src=inspect.getsource(b.fill_new)
        self.assertLess(src.index('douyin_focus.ensure(page)'),src.index('set_input_files'))
        self.assertIn('wait_player_ready(page, 180)',src)
    def test_wait_slices_and_reensures(self):
        import douyin_browser as b
        class TimeoutError_(Exception):pass
        TimeoutError_.__name__='TimeoutError'
        calls=[];ens=[]
        class P:
            def wait_for_function(self,js,timeout):
                calls.append(timeout)
                if len(calls)<3:raise TimeoutError_()
        orig=b.douyin_focus.ensure;b.douyin_focus.ensure=lambda p:ens.append(1)
        try:b.wait_player_ready(P(),180)
        finally:b.douyin_focus.ensure=orig
        self.assertEqual(len(calls),3);self.assertEqual(len(ens),2);self.assertTrue(all(c<=15000 for c in calls))
    def test_wait_raises_non_timeout(self):
        import douyin_browser as b
        class P:
            def wait_for_function(self,js,timeout):raise ValueError('x')
        with self.assertRaises(ValueError):b.wait_player_ready(P(),5)


class IdentityTabFocusTests(unittest.TestCase):
    def test_identity_tab_holds_focus_and_waits_longer(self):
        import douyin_browser as b,inspect
        src=inspect.getsource(b.assert_identity)
        self.assertLess(src.index('douyin_focus.hold(identity)'),src.index('identity.goto('))
        self.assertIn("timeout=30000",src)
        self.assertIn('账号编号未在 30 秒内出现',src)
