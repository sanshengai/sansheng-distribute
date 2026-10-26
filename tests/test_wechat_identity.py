import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlencode

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from publish_core import PublishError
from wechat_identity import load_link_identities
from wechat_adapter import article_equivalent
from write_bridge import comparison_expected


class IdentityTests(unittest.TestCase):
    def test_block_indentation_changes_but_inline_word_join_is_rejected(self):
        source={'content':'<section>标题</section>\n <section><span>Hello</span> <span>world</span></section>'}
        saved={'content':'<section>标题</section><section><span>Hello</span> <span>world</span></section>'}
        self.assertTrue(article_equivalent(source,saved))
        joined={'content':saved['content'].replace('</span> <span>','</span><span>')}
        self.assertFalse(article_equivalent(source,joined))
        self.assertFalse(article_equivalent(source,{'content':saved['content'].replace('world','other')}))

    def test_write_projection_is_explicit_and_preserves_source(self):
        expected={'content':'<p>说明</p><a href="https://example.com/doc">文档</a>'}
        projected, evidence=comparison_expected(expected,{'accounts':{'fixture':{'external_link_policy':'visible_url'}}},'fixture')
        self.assertIn('文档（https://example.com/doc）',projected['content'])
        self.assertIn('<a href=',expected['content'])
        self.assertEqual(evidence['source_sha256'],hashlib.sha256(expected['content'].encode()).hexdigest())
        strict, evidence=comparison_expected(expected,{},'fixture')
        self.assertIs(strict,expected);self.assertIsNone(evidence)
        with self.assertRaises(PublishError):comparison_expected(expected,{'accounts':{'fixture':{'external_link_policy':'unknown'}}},'fixture')
    def test_lazy_image_resize_preserves_identity_but_replacement_fails(self):
        source={'content':'<p>同一素材</p><img src="https://mmbiz.qpic.cn/mmbiz_png/opaqueSource/0?from=appmsg">'}
        actual={'content':'<p>同一素材</p><img data-src="https://mmbiz.qpic.cn/mmbiz_png/opaqueSource/640?from=appmsg">'}
        self.assertTrue(article_equivalent(source,actual))
        actual['content']=actual['content'].replace('opaqueSource','anotherImage')
        self.assertFalse(article_equivalent(source,actual))

    def test_short_link_requires_bound_official_literal_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);request=root/'request.json';proof=root/'source.html'
            source='https://mp.weixin.qq.com/s/fixtureSlug'
            body="var biz = 'fixtureBiz==';var mid = '123';var idx = '1';var sn = '"+'a'*32+"';var msg_link = '"+source+"';"
            proof.write_text(body)
            row=dict(source_url=source,response_url=source,status=200,path=str(proof),sha256=hashlib.sha256(proof.read_bytes()).hexdigest())
            payload=dict(manifest_digest='manifest-fixture',remote_id='draft-fixture',observations=[row])
            path=request.with_suffix('.wechat-links.json');path.write_text(json.dumps(payload))
            identities=load_link_identities(request,'manifest-fixture','draft-fixture')
            long='https://mp.weixin.qq.com/s?'+urlencode(dict(__biz='fixtureBiz==',mid='123',idx='1',sn='a'*32,scene='21'))+'#wechat_redirect'
            expected={'content':f'<a href="{source}">阅读</a>'};actual={'content':f'<a href="{long}">阅读</a>'}
            self.assertFalse(article_equivalent(expected,actual))
            self.assertTrue(article_equivalent(expected,actual,identities))
            self.assertFalse(article_equivalent(expected,{'content':actual['content'].replace('mid=123','mid=999')},identities))
            with self.assertRaises(PublishError):load_link_identities(request,'other-manifest','draft-fixture')
            proof.write_text(body+'changed')
            with self.assertRaises(PublishError):load_link_identities(request,'manifest-fixture','draft-fixture')

    def test_captcha_and_empty_proof_never_resolve_a_short_link(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);req=root/'request.json';proof=root/'source.html';proof.write_text('')
            row=dict(source_url='https://mp.weixin.qq.com/s/fixture',response_url='https://mp.weixin.qq.com/mp/captcha',status=200,path=str(proof),sha256=hashlib.sha256(b'').hexdigest())
            data=dict(manifest_digest='fixture',remote_id='fixture',observations=[row]);req.with_suffix('.wechat-links.json').write_text(json.dumps(data))
            with self.assertRaises(PublishError):load_link_identities(req,'fixture','fixture')
            row['response_url']=row['source_url'];req.with_suffix('.wechat-links.json').write_text(json.dumps(data))
            with self.assertRaises(PublishError):load_link_identities(req,'fixture','fixture')
