"""回归：YouTube 可重排标签，但不得漏掉或改变标签。"""
import json
from unittest import TestCase

import test_youtube_adapter as fixtures
from publish_core import PublishError, digest, validate
from youtube_adapter import tags_equal


class YoutubeTagOrderTest(TestCase):
    setUp = fixtures.YoutubeAdapterTest.setUp
    run_adapter = fixtures.YoutubeAdapterTest.run_adapter
    def prepare_tags(self):
        target=self.manifest['targets'][0]
        target['tags']=['初中英语','英语学习','校园短剧','英语听力','初馨']
        validate(self.manifest,sealing=True)
        self.mpath.write_text(json.dumps(self.manifest))
        self.request['manifest_digest']=digest(self.manifest)
        self.request['target']=target
        self.rpath.write_text(json.dumps(self.request))
        self.run_adapter('execute')

    def remote_tags(self, tags):
        original=self.fake.call
        def modified(method, url, **kwargs):
            code, headers, body = original(method, url, **kwargs)
            if '/videos?' in url and method == 'GET':
                response=json.loads(body)
                response['items'][0]['snippet']['tags']=tags
                body=json.dumps(response).encode()
            return code, headers, body
        self.fake.call=modified

    def test_actual_video_readback_allows_reordered_tags(self):
        self.prepare_tags()
        self.remote_tags(['初中英语','初馨','校园短剧','英语听力','英语学习'])
        self.assertEqual(self.run_adapter('verify')['state'],'published')

    def test_actual_video_readback_rejects_missing_tags(self):
        self.prepare_tags()
        self.remote_tags(['初中英语','初馨','校园短剧','英语听力'])
        with self.assertRaises(PublishError): self.run_adapter('verify')

    def test_tags_do_not_allow_substitution_duplicates_or_invalid_type(self):
        self.assertFalse(tags_equal(['a','c'], ['a','b']))
        self.assertFalse(tags_equal(['a','b','b'], ['a','b']))
        self.assertFalse(tags_equal('a', ['a']))
