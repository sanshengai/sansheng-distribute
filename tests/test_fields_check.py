import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import fields_check as fc

MATRIX = json.loads((Path(__file__).resolve().parent.parent / 'assets' / 'platform-fields.json').read_text(encoding='utf-8'))

def item(**md):
    return dict(id='S9E01', title='t', description='d', tags=['a'], cover='c.png', platform_metadata=md)

def test_complete_weibo_passes():
    r = fc.check([item(creation_type='原创', declaration='内容由AI生成', ai_generated=True)], MATRIX, 'weibo')
    assert r['ok'] and r['items'][0]['missing'] == []
    assert 'collection' in r['unsupported']

def test_missing_topics():
    it = item(creation_type='原创', ai_generated=True); it['tags'] = []
    r = fc.check([it], MATRIX, 'weibo')
    assert not r['ok'] and r['items'][0]['missing'] == ['topics']

def test_missing_collection_douyin():
    r = fc.check([item(ai_generated=True)], MATRIX, 'douyin')
    assert not r['ok'] and 'collection' in r['items'][0]['missing']
    r2 = fc.check([item(ai_generated=True, collection_name='初馨')], MATRIX, 'douyin')
    assert r2['ok']

def test_bilibili_cli_flags_unsupported():
    r = fc.check([item(copyright=1, ai_generated=True)], MATRIX, 'bilibili')
    assert r['ok'] and r['platform'] == 'bilibili_cli'
    assert {'native_topic', 'collection', 'activity'} <= set(r['unsupported'])
    w = ' '.join(r['items'][0]['warnings'])
    assert 'collection' in w and 'native_topic' in w

def test_original_option_unset():
    r = fc.check([item(collection_name='x', ai_generated=True)], MATRIX, 'wechat_channels')
    assert not r['ok'] and r['items'][0]['missing'] == ['original']

def test_platform_without_original_option_not_required():
    r = fc.check([item(ai_generated=True, collection_name='初馨')], MATRIX, 'douyin')
    assert r['ok'] and 'original' not in r['items'][0]['missing']
    x = fc.check([dict(id='S9E01', text='hello', tags=['英语学习'])], MATRIX, 'x')
    assert x['ok']

def test_set_but_unsupported_warns():
    r = fc.check([item(copyright=1, ai_generated=True, collection_name='初馨')], MATRIX, 'bilibili')
    assert any('已填写但该路线不支持' in w for w in r['items'][0]['warnings'])

def test_cli_exit_codes(tmp_path):
    p = tmp_path / 'c.json'
    p.write_text(json.dumps({'items': [item(ai_generated=True)]}), encoding='utf-8')
    assert fc.main(['--platform', 'douyin', '--copy', str(p)]) == 1
    assert fc.main(['--platform', 'nope', '--copy', str(p)]) == 2

def test_explicit_false_ai_is_a_decision():
    it = dict(id='S9E01', title='t', description='d', tags=['a'], cover='c', platform_metadata=dict(altered_content=False, playlist='PLx'))
    r = fc.check([it], MATRIX, 'youtube')
    assert r['ok'] and any('显式声明不含AI' in w for w in r['items'][0]['warnings'])
