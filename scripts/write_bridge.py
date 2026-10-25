"""Read back one existing draft and return a compatible, evidence-bound Write receipt.

Never uploads, creates a draft, changes the remote article, or overwrites an old
Write receipt. Write owns the subsequent local state transition and archive.
"""
from __future__ import annotations
import fcntl
import hashlib
from pathlib import Path
import sys
import secrets

from publish_core import (PublishError, adapter_config, digest, file_hash, invoke,
                          now, read_json, target_of, validate, write_json)
from wechat_adapter import normalize_legacy_anchors
from wechat_media import reconcile


def return_to_write(store, key, profile, article_dir, write_scripts):
    scripts = Path(write_scripts).expanduser().resolve()
    if not (scripts/'release_to_draft.py').is_file():
        raise PublishError('缺 Write 的现役 scripts 目录')
    sys.path.insert(0, str(scripts))
    import evidence
    import release_to_draft as release

    job = store.status(key)
    manifest = read_json(store.directory/'snapshots'/f'{key}.json')
    validate(manifest)
    root = Path(article_dir).expanduser().resolve()
    if manifest.get('producer') != 'sansheng-write' or Path(manifest['source_root']).resolve() != root:
        raise PublishError('回传只能交给原 Write 文章目录')
    if digest(manifest) != job['manifest_digest']:
        raise PublishError('回传快照与账本版本不符')
    target = target_of(manifest, job['target_id'])
    if (target['platform'], target['content_type'], target['endpoint']) != ('wechat','article','draft'):
        raise PublishError('此回传入口只处理公众号文章草稿')
    if not job.get('remote_id') or job['state'] not in {'pending','draft_ready'}:
        raise PublishError('尚无已读回的目标草稿；先 resume 核实')
    ready, errors = evidence.verify_publish_ready(root)
    expected, more = release.build_expected_draft(root)
    if errors or more or not ready or not expected:
        raise PublishError('Write 封存已失效：'+'; '.join(errors+more))
    # A different legacy remote identity must never be silently superseded.
    for prefix in ('', '过程记录'):
        for name in ('_release-attempt.json','_publish-receipt.json','_external-publish-receipt.json'):
            old = root/prefix/name
            if old.exists() and read_json(old).get('draft_media_id') != job['remote_id']:
                raise PublishError('Write 已绑定另一草稿；不能覆盖其发布记录')
    lock_dir = store.directory/'locks'; lock_dir.mkdir(exist_ok=True)
    lock = lock_dir/(digest({'platform':target['platform'],'account':target['account']})+'.lock')
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublishError('账号仍有活跃发布进程；不能并发回传') from exc
        owner = store.db.execute('SELECT job_key FROM account_locks WHERE platform=? AND account=?',
                                 (target['platform'],target['account'])).fetchone()
        if owner and owner['job_key'] != key:
            raise PublishError('账号另一任务的结果尚未核实；不能回传')
        request_path = store.directory/'requests'/f'{key}.json'
        request = dict(schema_version=1,manifest_path=str(store.directory/'snapshots'/f'{key}.json'),
                       manifest_digest=job['manifest_digest'],target=target,job=job,
                       verification_nonce=secrets.token_hex(16),
                       document_snapshot_path=str(store.directory/'snapshots'/f'{key}.documents.json'))
        write_json(request_path,request)
        with store.measure(key,'verify'):
            observation = invoke(adapter_config(profile,target,readonly=True),'verify',request_path,lock_fd=stream.fileno())
            store.save_observation(key,observation,verified=True)
        if observation.get('state') != 'draft_ready':
            raise PublishError('草稿尚未完成媒体/视觉核验，不能回传成功')
        raw = read_json(request_path.with_suffix('.readback.json'))
        if (raw.get('manifest_digest') != job['manifest_digest'] or raw.get('remote_id') != job['remote_id']
                or raw.get('verification_nonce') != request['verification_nonce']):
            raise PublishError('当前官方读回证据与任务不符')
        actual = raw['article']
        native = read_json(request_path.with_suffix('.native-media.json'))
        slots = [s for s in manifest.get('media_slots',[]) if s['document_id']==target['document_id']]
        restored = normalize_legacy_anchors(actual.get('content',''),slots)
        if slots:
            restored, remaining = reconcile(restored,slots,{a['id']:a for a in manifest['assets']},native,
                                           manifest_digest=job['manifest_digest'],remote_id=job['remote_id'])
            if remaining:
                raise PublishError('媒体核验仍有未完成项')
        for slot in slots:
            anchor = slot.get('source_anchor') or slot.get('legacy_anchor')
            token = '【发布媒体:'+slot['id']+'】'
            if not anchor or expected['content'].count(anchor)!=1 or restored.count(token)!=1:
                raise PublishError('缺唯一原稿锚点；先重新导出，不能猜回填位置')
            restored = restored.replace(token,anchor)
        compared = dict(actual,content=restored)
        checks, errors = release._compare_readback(expected,compared,str(actual.get('thumb_media_id') or ''))
        # The external path does not inherit the legacy reader's fuzzy drift allowance.
        if release._semantic_body_digest(expected['content']) != release._semantic_body_digest(restored):
            errors.append('外部回传正文与 Write 原稿不一致')
        if errors:
            raise PublishError('; '.join(errors))
        checks.pop('body_drift_tolerated',None)
        checks.update(native_media=True,layout=True)
        source_digest = evidence.stable_digest(ready['manifest'])
        receipt = dict(schema_version=2,sealed_at=now(),scope='wechat-draft',formal_publish=False,
                       draft_media_id=job['remote_id'],cover_media_id=actual['thumb_media_id'],
                       publish_ready_digest=ready['manifest_digest'],expected_draft_digest=evidence.stable_digest(expected),
                       manifest=ready['manifest'],manifest_digest=source_digest,remote_verified=True,
                       remote_readback=dict(checks=checks,title=actual.get('title'),digest=actual.get('digest'),
                         source_url=actual.get('content_source_url',''),body_digest=release._semantic_body_digest(restored),
                         image_count=release._image_count(restored),image_sources=release._image_sources(restored),
                         thumb_media_id=actual['thumb_media_id'],remote_digest=evidence.stable_digest(actual)),
                       external_publisher=dict(schema_version=1,job_key=key,manifest_digest=job['manifest_digest'],
                         platform=target['platform'],account=target['account'],target_id=target['id'],
                         content_sha256=hashlib.sha256(actual['content'].encode()).hexdigest(),
                         source_files=[dict(path=r['path'],sha256=r['sha256']) for r in manifest['documents']+manifest['assets']]),
                       native_media=native)
        # A later readback must never invalidate an already imported receipt.
        destination = root/'过程记录'/'publish'/'returned'/key/request['verification_nonce']
        destination.mkdir(parents=True,exist_ok=True)
        proof = destination/'official-readback.json'
        write_json(proof,raw)
        receipt['external_publisher']['readback_evidence'] = dict(path=proof.relative_to(root).as_posix(),sha256=file_hash(proof))
        output = destination/'write-receipt.json'
        write_json(output,receipt)
        return dict(receipt=str(output),draft_media_id=job['remote_id'],state='draft_ready')
