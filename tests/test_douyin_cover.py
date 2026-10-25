"""Cover source binding, native conversion, saved identities and read-only recovery."""
import base64
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import douyin_cover as c
from publish_core import PublishError

JPEG='data:image/jpeg;base64,'+base64.b64encode(b'native-transcoded-image').decode()


class Node:
    def __init__(self,p,kind,role=None):self.p=p;self.kind=kind;self.role=role
    def count(self):
        if self.kind=='prompt':return int(self.p.prompt)
        if self.kind=='video':return int(self.p.video)
        if self.kind=='choose':return int(self.role not in self.p.already_custom)
        if self.kind=='modal':return int(self.p.dialog==self.role)
        return 1
    def is_visible(self):return self.count()==1
    def filter(self,**kw):return self
    def get_by_text(self,text,**kw):return Node(self.p,'choose',self.role)
    def locator(self,selector):
        return Node(self.p,'image' if selector=='img' else 'native' if 'notImg-' in selector
                    else 'file' if selector=='input.semi-upload-hidden-input' else 'uploader',self.role)
    def get_by_role(self,role,**kw):return Node(self.p,kw['name'],self.role)
    def evaluate_all(self,script):return [self.p.images[self.role]['src']]
    def get_attribute(self,name):return self.p.images[self.role]['src']
    def evaluate(self,script):
        if self.kind=='file':return not self.p.pending
        if self.kind=='image':return self.p.images[self.role]
        if self.kind=='native':
            return dict(src='data:image/jpeg;base64,d3Jvbmc=' if self.p.wrong_source else JPEG,
                        width=900 if self.role=='portrait' else 1200,height=1200 if self.role=='portrait' else 900)
        raise AssertionError(self.kind)
    def set_input_files(self,path,**kw):self.p.uploads.append((self.role,Path(path).read_bytes()))
    def click(self,**kw):
        p=self.p
        if self.kind=='choose':p.dialog=self.role;p.video=False
        elif self.kind=='完成':
            p.images[self.role]=dict(src=p.url(self.role),width=899 if self.role=='portrait' else 1200,
                                    height=1200 if self.role=='portrait' else 899,complete=True)
            if self.role=='portrait':p.prompt=True
            else:p.dialog=None
        elif self.kind=='设置横封面':
            if not p.prompt:
                p.images['portrait']=dict(src=p.url('portrait'),width=899,height=1200,complete=True)
            p.prompt=False;p.dialog='landscape'
        elif self.kind=='暂不设置':p.prompt=False;p.dialog=None
        elif self.kind=='video_tab':p.video=True
        else:raise AssertionError(self.kind)
    def wait_for(self,**kw):
        if self.kind=='prompt' and kw['state']=='hidden':assert not self.p.prompt
        if self.kind=='modal' and kw['state']=='hidden':assert self.p.dialog!=self.role
        if self.kind=='modal' and kw['state']=='visible':assert self.p.dialog==self.role


class Page:
    def __init__(self):
        self.uploads=[];self.dialog=None;self.prompt=False;self.video=True
        self.pending=False;self.wrong_source=False;self.already_custom=set()
        self.images={role:dict(src='https://example.org/default-'+role+'.jpg',width=10,height=10,complete=True) for role in c.ROLES}
    def url(self,role):return 'https://p0-creator-media-private.douyin.com/tos-cn-i-fixture/object_'+role+'~tplv-fixture-image.jpeg?expiring=1'
    def locator(self,selector):return Node(self,'video' if selector=='video' else 'prompt')
    def get_by_text(self,text,**kw):return Node(self,'video_tab')
    def wait_for_function(self,*a,**kw):pass


class CoverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.assets=[]
        for role in c.ROLES:
            path=self.root/(role+'.png');path.write_bytes((role+' bytes').encode())
            self.assets.append(dict(id=role,kind='image',path=path.name,absolute_path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        self.pack=dict(platform_metadata={'cover_assets':{'portrait':'portrait','landscape':'landscape'}},
                       cover_asset_id=None,assets=self.assets)
        self.page=Page();self.records={};self.phases=[]
        patcher=patch.object(c,'control',side_effect=lambda page,role:Node(page,'control',role));patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(c,'modal',side_effect=lambda page,role:Node(page,'modal',role));patcher.start();self.addCleanup(patcher.stop)
    def prepared(self,roles=('portrait','landscape')):
        return {role:dict(source_asset_id=role,source_sha256=next(x['sha256'] for x in self.assets if x['id']==role),
                         native_sha256=hashlib.sha256(b'native-transcoded-image').hexdigest(),jpeg=JPEG,
                         width=900 if role=='portrait' else 1200,height=1200 if role=='portrait' else 900) for role in roles}
    def upload(self,roles=('portrait','landscape')):
        c.upload(self.page,self.assets,self.prepared(roles),progress=self.phases.append,receipt=self.records.__setitem__)

    def test_two_covers_upload_in_native_order_and_readback(self):
        self.upload();self.assertEqual([r for r,_ in self.page.uploads],['portrait','landscape'])
        self.assertEqual(set(self.records),{'portrait','landscape'});self.assertTrue(self.page.video)
        before=list(self.page.uploads);c.verify(self.page,self.pack,{'covers':self.records})
        self.assertEqual(before,self.page.uploads)
        self.assertTrue(all('expiring' not in str(r) for r in self.records.values()))

    def test_portrait_only_does_not_create_unrequested_landscape(self):
        self.upload(('portrait',));self.assertEqual([r for r,_ in self.page.uploads],['portrait'])
        self.assertFalse(self.page.prompt);self.assertIsNone(self.page.dialog)
        self.assertEqual(set(self.records),{'portrait'})

    def test_landscape_only_does_not_create_unrequested_portrait(self):
        self.upload(('landscape',));self.assertEqual([r for r,_ in self.page.uploads],['landscape'])
        self.assertEqual(set(self.records),{'landscape'})

    def test_existing_custom_cover_stops_before_any_upload(self):
        self.page.already_custom.add('landscape')
        with self.assertRaisesRegex(PublishError,'空封面'):self.upload()
        self.assertEqual(self.page.uploads,[])

    def test_changed_snapshot_never_uploaded(self):
        Path(self.assets[0]['absolute_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(PublishError,'快照'):self.upload()
        self.assertEqual(self.page.uploads,[])

    def test_pending_file_preserved_without_new_upload(self):
        self.page.pending=True
        with self.assertRaisesRegex(PublishError,'待上传'):self.upload()
        self.assertEqual(self.page.uploads,[])

    def test_native_save_without_changed_cover_is_not_a_receipt(self):
        for role in c.ROLES:self.page.images[role]['src']=self.page.url(role)
        with self.assertRaisesRegex(PublishError,'未改变'):self.upload()
        self.assertEqual(self.records,{})

    def test_extra_native_crop_stops_instead_of_claiming_source_preserved(self):
        original=c.preview
        def cropped(page,role):
            value=original(page,role)
            if role=='portrait':value['width']-=5
            return value
        with patch.object(c,'preview',side_effect=cropped):
            with self.assertRaisesRegex(PublishError,'额外裁切'):self.upload()
        self.assertEqual(self.records,{})

    def test_native_wrong_source_blocks_save_and_receipt(self):
        self.page.wrong_source=True
        with self.assertRaisesRegex(PublishError,'实际源图'):self.upload()
        self.assertEqual(len(self.page.uploads),1);self.assertEqual(self.records,{})
        self.assertEqual(self.page.dialog,'portrait')

    def test_unknown_result_resume_never_uploads(self):
        with self.assertRaisesRegex(PublishError,'回执不完整'):c.verify(self.page,self.pack,{})
        self.assertEqual(self.page.uploads,[])

    def test_changed_saved_identity_missing_role_or_source_binding_rejected(self):
        self.upload();original=copy.deepcopy(self.records)
        cases=[{'portrait':original['portrait']},dict(original,extra=original['portrait']),
               dict(original,portrait=dict(original['portrait'],object_key='0'*64)),
               dict(original,portrait=dict(original['portrait'],source_sha256='0'*64)),
               dict(original,portrait=dict(original['portrait'],source_asset_id='landscape'))]
        for receipts in cases:
            with self.subTest(receipts=receipts),self.assertRaises(PublishError):c.verify(self.page,self.pack,{'covers':receipts})
        self.assertEqual(len(self.page.uploads),2)

    def test_rotated_signature_keeps_object_identity(self):
        self.upload();self.page.images['portrait']['src']=self.page.url('portrait').replace('expiring=1','expiring=2')
        c.verify(self.page,self.pack,{'covers':self.records});self.assertEqual(len(self.page.uploads),2)

    def test_saved_preview_untrusted_host_incomplete_or_bad_dimensions_rejected(self):
        self.upload();original=copy.deepcopy(self.page.images['portrait'])
        for changed in [dict(original,src=original['src'].replace('.douyin.com','.douyin.com.evil.example')),
                        dict(original,complete=False),dict(original,width=0),dict(original,src='https://p0-creator-media-private.douyin.com/unexpected')]:
            self.page.images['portrait']=changed
            with self.assertRaises(PublishError):c.preview(self.page,'portrait')
        self.page.images['portrait']=original

    def test_invalid_role_mapping_missing_or_duplicated_assets(self):
        for mapping in (None,[],{'square':'portrait'},{'portrait':'missing'},{'portrait':'portrait','landscape':'portrait'}, {'portrait':None}):
            pack=copy.deepcopy(self.pack);pack['platform_metadata']['cover_assets']=mapping
            with self.assertRaises(PublishError):c.requested(pack)
        pack=copy.deepcopy(self.pack);pack['cover_asset_id']='landscape'
        with self.assertRaisesRegex(PublishError,'冲突'):c.requested(pack)
        self.assertEqual(self.page.uploads,[])

    def test_single_cover_alias_is_portrait_and_empty_contract_stays_compatible(self):
        pack=copy.deepcopy(self.pack);pack['platform_metadata']={};pack['cover_asset_id']='portrait'
        self.assertEqual(c.requested(pack),{'portrait':'portrait'})
        c.verify(self.page,dict(pack,cover_asset_id=None),{})

    def test_decode_ratio_empty_encoding_or_large_dimensions_rejected_before_upload(self):
        good=dict(width=900,height=1200,jpeg=JPEG)
        for value in (dict(good,width=1000),dict(good,width=0),dict(good,width=4500,height=6000),dict(good,jpeg='data:image/jpeg;base64,')):
            self.page.evaluate=lambda *a,v=value:v
            with self.assertRaisesRegex(PublishError,'比例不符'):c.encode(self.page,self.assets[0],'portrait')
        self.assertEqual(self.page.uploads,[])
        self.page.evaluate=lambda *a:good
        self.assertEqual(c.encode(self.page,self.assets[0],'portrait')['source_sha256'],self.assets[0]['sha256'])


if __name__=='__main__':unittest.main()
