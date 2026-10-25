"""Opt-in native single-video sender; immutable inputs and durable one attempt."""
from __future__ import annotations
import os
import time
from urllib.parse import urlsplit

from browser_assets import stage
from browser_runtime import click_background
from platform_pack import build_pack
from publish_core import PublishError,digest,read_json,write_json
import weibo_browser as home
import weibo_publish_browser as shared
import weibo_video_browser as prefill
import weibo_video_fields as fields
import weibo_video_post as post
import weibo_video_submission as submission


def ready(page,pack,assets,uid,saved):
    prefill.video.assert_identity(page,uid);home.assert_no_warning(page)
    prefill.verify(page,pack,assets,saved)
    snapshot=submission.composer_state(page.evaluate(submission.COMPOSER),pack)
    snapshot['default_options']=submission.default_options(page)
    snapshot['cover_pid']=submission.cover_pid(page)
    return snapshot


class Capture(shared.Capture):
    def __init__(self,uid,pack,saved,marker,snapshot):
        super().__init__(uid,lambda value:shared.checkpoint(saved,marker,accepted=value,state='accepted'))
        self.pack,self.saved,self.marker,self.snapshot=pack,saved,marker,snapshot
        self.input_count=0;self.input_error=None

    def request(self,request):
        u=urlsplit(request.url)
        if u.scheme!='https' or u.netloc!='weibo.com' or u.path!='/ajax/statuses/update':return
        self.input_count+=1
        try:
            if self.input_count!=1:raise PublishError('微博原生视频出现多个发送请求')
            value=submission.native_input(request,self.pack,self.saved,self.snapshot)
            shared.checkpoint(self.saved,self.marker,native_input_receipt=value)
        except Exception:
            self.input_error='微博原生视频发送载荷未能完整核对或保存；不重复发送'


def submit_once(page,pack,assets,uid,saved,marker):
    if saved.get('submission_attempted_at') is not None or saved.get('accepted'):
        raise PublishError('微博视频已有发送尝试；只查询原作品，不再次点击')
    snapshot=ready(page,pack,assets,uid,saved)
    button=page.get_by_role('button',name='发布',exact=True)
    if button.count()!=1 or not button.is_visible() or button.is_disabled():
        raise PublishError('微博视频原生发布按钮不唯一、不可见或禁用')
    shared.checkpoint(saved,marker,submission_attempted_at=time.time(),state='submission_unknown',
                      submission_snapshot=snapshot)
    capture=Capture(uid,pack,saved,marker,snapshot)
    page.on('request',capture.request);page.on('response',capture.response)
    def before_input():
        if ready(page,pack,assets,uid,saved)!=snapshot:
            raise PublishError('微博视频末次输入前状态改变；保留原稿')
        shared.checkpoint(saved,marker,native_input_checkpointed_at=time.time())
    try:
        try:click_background(page,button,before_input=before_input)
        except Exception as exc:
            saved['click_error_type']=type(exc).__name__
            saved['click_error_phase']='native_input_uncertain' if saved.get('native_input_checkpointed_at') else 'before_native_input'
            if saved['click_error_phase']=='before_native_input':
                raise PublishError('微博视频原生点击前检查未完成；保留同一稿，不自动重试') from exc
            saved['click_reply_unknown']=True
        deadline=time.monotonic()+20
        while capture.accepted is None and capture.error is None and time.monotonic()<deadline:
            page.wait_for_timeout(100)
    finally:
        page.remove_listener('request',capture.request);page.remove_listener('response',capture.response)
        saved['native_response_count']=capture.count;saved['native_input_count']=capture.input_count
        if capture.error or capture.input_error:saved['receipt_error']=capture.error or capture.input_error
        write_json(marker,saved)
    if not saved.get('accepted'):
        raise PublishError('微博视频发送结果未知且没有可核对作品 ID；不重复发送')
    return saved['accepted']


def dispatch(request_path,action,profile,lock_fd):
    if action not in {'execute','verify'}:raise PublishError('不支持的微博视频发送阶段')
    try:os.fstat(lock_fd)
    except OSError as exc:raise PublishError('微博视频发送须继承核心账号锁') from exc
    request=read_json(request_path);manifest=read_json(request['manifest_path'])
    if digest(manifest)!=request['manifest_digest'] or request['target'] not in manifest.get('targets',[]):
        raise PublishError('微博视频发送请求与封存清单不符')
    target=request['target'];uid,endpoint,browser=home.settings(profile,target)
    if action=='execute' and profile['accounts'][target['account']].get('restricted'):
        raise PublishError('微博账号受限，只准备本地包')
    pack=build_pack(manifest,target['id'],profile,check_files=action=='execute');fields.check(pack,publishing=True)
    bound=dict(pack_digest=digest(pack),identity=uid,cdp_url=endpoint,browser_profile=browser)
    marker=home.marker_path(request_path)
    if action=='execute' and marker.exists():raise PublishError('微博视频任务已有尝试；只查询，不重填或重传')
    if action=='verify':
        saved=read_json(marker)
        if any(saved.get(k)!=v for k,v in bound.items()) or not saved.get('anchor_target_id'):
            raise PublishError('微博原视频发送绑定已改变')
        accepted=saved.get('accepted')
        if not saved.get('submission_attempted_at') or not isinstance(accepted,dict):
            raise PublishError('微博视频缺少原生作品 ID；保持未知，不重新发送')
        post.post.status_url(uid,accepted.get('remote_id'))
        if (accepted.get('identity')!=uid or request['job'].get('remote_id') not in (None,accepted['remote_id'])):
            raise PublishError('微博视频原作品或作者与核心账本矛盾')
        receipt=saved.get('native_input_receipt');snapshot=saved.get('submission_snapshot')
        if (saved.get('receipt_error') or saved.get('native_input_count')!=1 or saved.get('native_response_count')!=1
                or not saved.get('native_input_checkpointed_at') or not isinstance(receipt,dict)
                or not isinstance(snapshot,dict) or receipt.get('pack_digest')!=digest(pack)
                or receipt.get('statement')!=snapshot.get('statement') or receipt.get('cover_pid')!=snapshot.get('cover_pid')):
            raise PublishError('微博视频原生发送载荷或持久回执不完整；不重发或冒称成功')
    from browser_engine import target_id
    with home.connect(profile,writing=action=='execute') as connection:
        anchor=home.select_page(connection,'weibo',path='/',
            **(dict(target_id=saved['anchor_target_id']) if action=='verify' else {}))
        home.assert_identity(anchor,uid)
        if action=='execute':
            assets=stage(pack,request_path)
            with prefill.filled_editor(anchor,pack,assets,dict(bound,anchor_target_id=target_id(anchor)),marker,endpoint) as (page,saved):
                accepted=submit_once(page,pack,assets,uid,saved,marker)
                # Keep the owned editor for uncertain/invalid native receipts.
                if not saved.get('receipt_error') and saved.get('native_input_receipt'):
                    try:page.close();saved['owned_composer_closed_after_acceptance']=True
                    except Exception as exc:saved['owned_composer_cleanup_error']=type(exc).__name__
                    write_json(marker,saved)
            return dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},
                state='pending',remote_id=accepted['remote_id'],remote_url=post.post.status_url(uid,accepted['remote_id']),
                observed_account_identity=uid,tracking_scope='native_post_id')
        observed=post.observe_existing(anchor,uid,accepted['remote_id'],endpoint)
        sources=post.bind_sources(pack,uid,accepted,saved,observed)
        if receipt['media_id']!=sources['native_media_id'] or receipt['cover_pid']!=observed['public']['cover_pid']:
            raise PublishError('微博视频实际发送媒体与独立作品不一致')
        confirmed=observed['public']['statement'] is not None and observed['public']['statement']==receipt['statement']
        result=dict({k:pack[k] for k in ('manifest_digest','target_id','platform','account')},**sources,
            state='published' if confirmed else 'pending',remote_url=post.post.status_url(uid,accepted['remote_id']),
            observed_account_identity=uid,tracking_scope='native_post_id',content_verified=confirmed,
            declaration_verified=confirmed)
        if confirmed:result['verified_asset_ids']=[a['id'] for a in pack['assets']]
        else:result['pending_reason']='原生公开作品未返回可核对的独立内容声明，不推断为已声明或无声明'
        write_json(marker.with_suffix('.weibo-video-readback.json'),dict(observed=observed,result=result))
        return result
