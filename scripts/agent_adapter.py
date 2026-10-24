#!/usr/bin/env python3
"""Local mailbox for the Agent's permitted browser tool. Never drives a browser itself."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import sys
import time

from publish_core import PublishError, digest, file_hash, now, read_json, write_json


def paths(request_path):
    root=Path(request_path).with_suffix('.agent')
    return root,root/'task.json',root/'reply.json',root/'lease.lock'


def inspect(request_path):
    root,task_path,reply_path,lease_path=paths(request_path)
    task=read_json(task_path)
    if task.get('status')!='active':
        raise PublishError('浏览器接力已结束；不可继续远端写入')
    with lease_path.open('a') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return task
        raise PublishError('接力进程已退出；先恢复任务，不可继续远端写入')


def check_reply(task, envelope):
    if envelope.get('nonce')!=task['nonce']:
        raise PublishError('回应属于旧接力阶段')
    observation=envelope.get('observation',{})
    request=task['request']; target=request['target']
    for field,value in dict(manifest_digest=request['manifest_digest'],target_id=target['id'],
                            platform=target['platform'],account=target['account']).items():
        if observation.get(field)!=value:
            raise PublishError(f'接力身份不匹配：{field}')
    previous=request['job'].get('remote_id')
    if previous and observation.get('remote_id')!=previous:
        raise PublishError('接力不能更换已有远端任务')
    if task['action']=='execute' and observation.get('state')!='pending':
        raise PublishError('执行阶段只能登记 pending；完成状态来自独立读回')
    evidence=envelope.get('evidence',[])
    if not isinstance(evidence,list) or not evidence:
        raise PublishError('接力回应必须附本阶段实际观察证据')
    for item in evidence:
        path=Path(item.get('path',''))
        if not path.is_absolute() or not path.is_file() or not path.stat().st_size or file_hash(path)!=item.get('sha256'):
            raise PublishError('证据缺失、为空或内容已变化')
    if envelope.get('observed_phase')!=task['nonce']:
        raise PublishError('验证须重新观察当前阶段；不可复用执行阶段的证据')
    observation=dict(observation,evidence=evidence,agent_phase=task['nonce'])
    return observation


def reply(request_path,envelope):
    task=inspect(request_path)
    check_reply(task,envelope)
    _,_,reply_path,_=paths(request_path)
    if reply_path.exists():
        raise PublishError('本阶段已有回应，等待执行器接收；不可覆盖')
    # O_EXCL prevents competing responders from replacing each other's evidence.
    payload=(json.dumps(envelope,ensure_ascii=False,indent=2)+'\n').encode()
    fd=os.open(reply_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream: stream.write(payload)
    return dict(reply_written=True,phase=task['nonce'])


def dispatch(request_path,action,*,timeout=1800):
    root,task_path,reply_path,lease_path=paths(request_path)
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    request=read_json(request_path)
    manifest=read_json(request['manifest_path'])
    if digest(manifest)!=request['manifest_digest']:
        raise PublishError('浏览器接力快照指纹不符')
    with lease_path.open('a') as lease:
        try: fcntl.flock(lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc: raise PublishError('已有浏览器接力在运行') from exc
        if reply_path.exists(): reply_path.unlink()  # Only our consumed phase mailbox, not evidence.
        task=dict(schema_version=1,status='active',action=action,nonce=secrets.token_hex(16),
                  started_at=now(),pid=os.getpid(),request=request)
        write_json(task_path,task)
        deadline=time.monotonic()+timeout
        try:
            while time.monotonic()<deadline:
                if reply_path.exists():
                    try: envelope=read_json(reply_path)
                    except PublishError:
                        time.sleep(.05); continue  # Exclusive writer may still be flushing.
                    observation=check_reply(task,envelope)
                    write_json(root/f"{task['nonce']}.receipt.json",envelope)
                    task['status']='responded'
                    return observation
                time.sleep(.2)
            raise PublishError('浏览器接力超时；状态未知，先查询，禁止重复提交')
        finally:
            task['status']='responded' if task['status']=='responded' else 'closed'
            task['ended_at']=now(); write_json(task_path,task)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['execute','verify','inspect','reply'])
    p.add_argument('--request',required=True)
    p.add_argument('--response')
    p.add_argument('--timeout',type=float,default=1800)
    args=p.parse_args()
    try:
        if args.action=='inspect': result=inspect(args.request)
        elif args.action=='reply':
            if not args.response: raise PublishError('reply 需要 --response')
            result=reply(args.request,read_json(args.response))
        else:
            if 'SANDY_PUBLISH_LOCK_FD' not in os.environ:
                raise PublishError('只能由发布运行时在账号锁内调用')
            os.fstat(int(os.environ['SANDY_PUBLISH_LOCK_FD']))
            result=dispatch(args.request,args.action,timeout=args.timeout)
        print(json.dumps(result,ensure_ascii=False)); return 0
    except (PublishError,OSError,ValueError) as exc:
        print(json.dumps(dict(ok=False,error=str(exc)),ensure_ascii=False),file=sys.stderr); return 2


if __name__=='__main__': raise SystemExit(main())
