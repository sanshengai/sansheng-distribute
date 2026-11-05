import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import alipay_list_guard as g
URL='https://c.alipay.com/page/content-creation/posts?appId=1'
def resp(rows):return {'stat':'ok','result':{'total':len(rows),'publishContents':rows}}
def row(i,t):return {'contentId':i,'title':t,'state':'已发布'}
M1='第 1 集：盘古开天';M10='第 10 集：良渚'
class SeriesMatch(unittest.TestCase):
    def test_default_still_chuxin(self):
        self.assertEqual(g.title_key('S6E01'),'「初馨」S6E01｜')
        rs=[resp([row('a','「初馨」S6E01｜第一页')])]
        self.assertEqual(g.evaluate(URL,True,rs,'S6E01')['status'],g.DUPLICATE)
    def test_new_series_match(self):
        rs=[resp([row('a','《上下五千年 | 第 1 集：盘古开天》'),row('b','「初馨」S6E01｜第一页')])]
        self.assertEqual(g.evaluate(URL,True,rs,'SX01',match=M1)['status'],g.DUPLICATE)
        self.assertEqual(g.evaluate(URL,True,rs,'SX10',match=M10)['status'],g.CLEAR)
    def test_ep1_does_not_hit_ep10(self):
        rs=[resp([row('a','《上下五千年 | 第 10 集：良渚》')])]
        self.assertEqual(g.evaluate(URL,True,rs,'SX01',match=M1)['status'],g.CLEAR)
    def test_readback_and_series(self):
        rs=[resp([row('a','《上下五千年 | 第 1 集：盘古开天》')])]
        self.assertEqual(g.evaluate(URL,True,rs,'SX01',expected_new=1,match=M1)['status'],g.CLEAR)
        r=g.evaluate_series(URL,True,rs,['SX01','SX10'],matches={'SX01':M1,'SX10':M10})
        self.assertEqual(r['status'],g.DUPLICATE);self.assertEqual(len(r['per_ep']['SX01']),1)
    def test_empty_match_rejected(self):
        with self.assertRaises(ValueError):g.title_key('SX01','  ')
if __name__=='__main__':unittest.main()
