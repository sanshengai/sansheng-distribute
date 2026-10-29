import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bilibili_comment_readback as b
from publish_core import PublishError

UID, AID, BV = '41', '29', 'BV1AA411A7gJ'


def config():
    return {'enabled': True, 'account': 'creator', 'uid': UID,
            'credentials': '/private/auth.json', 'not_before': '2020-01-01T00:00:00Z',
            'works': [{'aid': AID, 'bvid': BV}]}


def comment(rid=1, *, root=0, parent=0, author=41, text='这一句可以跟读。'):
    return {'rpid': rid, 'oid': 29, 'mid': author, 'type': 1, 'root': root, 'parent': parent,
            'state': 0, 'invisible': False, 'ctime': 1701241993,
            'member': {'mid': str(author), 'uname': 'example'}, 'content': {'message': text}}


def probe(**overrides):
    return {'schema': 'bilibili-comment-probe.v1', 'account': 'creator', 'uid': UID,
            'aid': AID, 'bvid': BV, 'comment_id': '1', 'root_id': '0', 'parent_id': '0',
            'text': '这一句可以跟读。', **overrides}


class Fake:
    def __init__(self):
        self.root = comment()
        self.children = []
        self.roots = [copy.deepcopy(self.root)]
        self.uid = UID
        self.owner = UID
        self.calls = []
        self.mutate = lambda kind, params, data: None

    def get(self, kind, **params):
        self.calls.append((kind, params))
        if kind == 'identity':
            return {'isLogin': True, 'mid': self.uid}
        if kind == 'work':
            return {'aid': AID, 'bvid': BV, 'owner': {'mid': self.owner}}
        assert kind in ('threads', 'replies')
        pn = params['pn']
        rows = self.roots if kind == 'threads' else self.children
        raw = {'page': {'num': pn, 'size': 20, 'count': len(rows)},
               'replies': copy.deepcopy(rows[(pn - 1) * 20:pn * 20])}
        if kind == 'replies':
            raw['root'] = copy.deepcopy(self.root)
        self.mutate(kind, params, raw)
        return raw


class ReadbackTests(unittest.TestCase):
    def test_root_requires_identity_and_list_readback(self):
        f = Fake(); r = b.readback(config(), probe(), f)
        self.assertEqual(r['state'], 'visible_on_readback')
        self.assertTrue(r['verified_public'])
        self.assertTrue(r['root_on_list'])
        self.assertFalse(r['may_resend'])
        self.assertEqual(r['writes'], 0)
        self.assertEqual([x[0] for x in f.calls], ['identity', 'work', 'replies', 'threads'])

    def test_hidden_id_is_not_success_even_with_matching_body(self):
        for flag, value in [('state', 17), ('invisible', True), ('state', 3)]:
            with self.subTest(flag=flag, value=value):
                f = Fake(); f.root[flag] = value
                r = b.readback(config(), probe(), f)
                self.assertEqual(r['state'], 'restricted')
                self.assertFalse(r['verified_public'])
                self.assertFalse(r['may_resend'])

    def test_direct_id_read_without_list_presence_stays_unconfirmed(self):
        f = Fake(); f.roots = []
        r = b.readback(config(), probe(), f)
        self.assertEqual(r['state'], 'unconfirmed')
        self.assertFalse(r['verified_public'])
        self.assertTrue(r['list_complete'])

    def test_child_keeps_distinct_root_and_direct_parent(self):
        f = Fake(); f.root = comment(author=66); f.roots = [f.root]
        f.children = [comment(2, root=1, parent=1, author=88), comment(99, root=1, parent=2)]
        r = b.readback(config(), probe(comment_id='99', root_id='1', parent_id='2'), f)
        self.assertTrue(r['verified_public'])
        self.assertEqual(r['parent_id'], '2')
        self.assertEqual(f.calls[2][1]['root'], '1')

    def test_wrong_child_parent_rejected(self):
        f = Fake(); f.children = [comment(99, root=1, parent=1)]
        with self.assertRaises(PublishError):
            b.readback(config(), probe(comment_id='99', root_id='1', parent_id='2'), f)

    def test_hidden_root_blocks_normal_child(self):
        f = Fake(); f.root['state'] = 17; f.children = [comment(99, root=1, parent=1)]
        r = b.readback(config(), probe(comment_id='99', root_id='1', parent_id='1'), f)
        self.assertEqual(r['state'], 'restricted')
        self.assertFalse(r['verified_public'])

    def test_wrong_login_or_work_owner_stops_before_comment_queries(self):
        for field, calls in [('uid', 1), ('owner', 2)]:
            f = Fake(); setattr(f, field, '999')
            with self.assertRaises(PublishError): b.readback(config(), probe(), f)
            self.assertEqual(len(f.calls), calls)

    def test_wrong_root_body_or_author_rejected(self):
        for field, value in [('rpid', 9), ('content', {'message': 'other'}), ('mid', 42)]:
            f = Fake(); f.root[field] = value
            with self.assertRaises(PublishError): b.readback(config(), probe(), f)

    def test_probe_rejects_empty_out_of_scope_and_inconsistent_ids_without_network(self):
        for override in ({'comment_id': ''}, {'aid': '30'}, {'root_id': '1'},
                         {'root_id': '1', 'parent_id': '1'}, {'text': ''}, {'uid': False},
                         {'parent_id': False}, {'extra': 'field'}):
            f = Fake()
            with self.assertRaises(PublishError): b.readback(config(), probe(**override), f)
            self.assertEqual(f.calls, [])
        cfg = config(); cfg['enabled'] = False; f = Fake()
        with self.assertRaises(PublishError): b.readback(cfg, probe(), f)
        self.assertEqual(f.calls, [])

    def test_missing_visibility_is_not_assumed_normal(self):
        for field, value in [('state', None), ('state', False), ('invisible', 0)]:
            f = Fake(); f.root[field] = value
            with self.assertRaises(PublishError): b.readback(config(), probe(), f)

    def test_child_pagination_and_budget_are_explicit(self):
        f = Fake(); f.children = [comment(i, root=1, parent=1, author=99) for i in range(2, 22)]
        f.children += [comment(99, root=1, parent=1)]
        p = probe(comment_id='99', root_id='1', parent_id='1')
        r = b.readback(config(), p, f, max_pages=1)
        self.assertFalse(r['scan_complete'])
        self.assertFalse(r['verified_public'])
        r = b.readback(config(), p, f, max_pages=2)
        self.assertTrue(r['verified_public'])
        self.assertEqual(r['pages'], 2)

    def test_wrong_page_duplicate_or_changed_root_rejected(self):
        for case in ('page', 'duplicate', 'root'):
            f = Fake(); f.children = [comment(i, root=1, parent=1) for i in range(2, 23)]
            def mutate(kind, params, data):
                if kind != 'replies': return
                if case == 'page': data['page']['num'] = 9
                if params['pn'] == 2:
                    if case == 'duplicate': data['replies'] = [comment(2, root=1, parent=1)]
                    if case == 'root': data['root']['content']['message'] = 'changed'
            f.mutate = mutate
            with self.assertRaises(PublishError):
                b.readback(config(), probe(comment_id='99', root_id='1', parent_id='1'), f)

    def test_list_visibility_cannot_conflict_with_direct_read(self):
        f = Fake(); f.roots[0]['state'] = 17
        with self.assertRaises(PublishError): b.readback(config(), probe(), f)


if __name__ == '__main__':
    unittest.main()
