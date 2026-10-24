#!/usr/bin/env python3
"""Agent-executed social platforms with lossless work orders and optional pinned X prefill."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import agent_adapter
from environment import load_profile
from platform_pack import build_pack
from publish_core import PublishError, digest, read_json, write_json

BAOYU_COMMIT='1567581c26ec29f4216c6e6835415bf30343b0e3'
BAOYU_SCRIPTS={'text':'skills/baoyu-post-to-x/scripts/x-browser.ts','images':'skills/baoyu-post-to-x/scripts/x-browser.ts','video':'skills/baoyu-post-to-x/scripts/x-video.ts'}


def _account(profile,target,*,readonly=False):
    account=profile.get('accounts',{}).get(target['account'])
    if not isinstance(account,dict) or target['platform'] not in account.get('platforms',[]): raise PublishError('账号未绑定目标平台')
    if account.get('restricted') and not readonly: raise PublishError('账号受限，只准备本地包')
    identity=account.get('public_identities',{}).get(target['platform'])
    if not isinstance(identity,str) or not identity.strip(): raise PublishError('需要公开账号身份 public_identities，用于与实际页面核对')
    browser=profile.get('browser_profile')
    if not isinstance(browser,str) or not Path(browser).is_absolute(): raise PublishError('需要专用 browser_profile 绝对路径')
    return identity,str(Path(browser).resolve())


def work_path(request_path):
    return Path(request_path).with_suffix('.social.json')


def validate_observation(observation,pack,identity,action):
    if observation.get('observed_account_identity')!=identity: raise PublishError('实际页面账号身份与配置不符')
    for key in ('manifest_digest','target_id','platform','account'):
        if observation.get(key)!=pack[key]: raise PublishError(f'社交回执身份不符：{key}')
    if action=='execute' and observation.get('state')!='pending': raise PublishError('执行不能替代独立读回')
    if action=='verify' and observation.get('state') in {'draft_ready','prefill_ready','published'}:
        expected={'draft':'draft_ready','prefill':'prefill_ready','publish':'published'}[pack['endpoint']]
        if observation['state']!=expected: raise PublishError('回执终点与授权终点不符')
        if observation.get('verified_pack_digest')!=digest(pack): raise PublishError('读回未覆盖当前完整发布包')
        expected_assets={x['id'] for x in pack['assets']}
        verified=observation.get('verified_asset_ids',[])
        if not isinstance(verified,list) or any(not isinstance(x,str) for x in verified) or len(verified)!=len(set(verified)) or set(verified)!=expected_assets: raise PublishError('读回素材、封面或字幕不完整')
        if observation.get('content_verified') is not True or not observation.get('remote_id'): raise PublishError('完成必须有真实可恢复身份和内容读回')
    return observation


def pinned_prefill_command(pack,profile):
    """Only trusted configuration supplies executables; content never supplies CLI flags."""
    cfg=profile.get('adapters',{}).get('x',{}).get('upstream_prefill',{})
    if cfg.get('provider')!='baoyu-x' or pack['platform']!='x' or pack['endpoint']!='prefill' or pack['content_type'] not in BAOYU_SCRIPTS:
        raise PublishError('宝玉桥只支持 X text/images/video 的 prefill')
    if pack['cover_asset_id'] or pack['subtitle_asset_ids'] or pack['platform_metadata'] or pack['document'] and pack['content_type']=='article':
        raise PublishError('上游预填不承接封面、字幕、平台额外字段或文章；改用 Agent 完整操作')
    if pack['fields']['description'] and pack['fields']['text'] and pack['fields']['description']!=pack['fields']['text']:
        raise PublishError('上游只有一个文案字段；不能丢弃独立 description')
    # Upstream positional parser treats a leading '-' as a flag, including --submit.
    body=pack['fields']['composed_text']
    if body.startswith('-') or body!=body.strip(): raise PublishError('上游会改写或误解析该文案，改用 Agent 输入')
    if pack['fields']['title'] and pack['fields']['title']!=body: raise PublishError('X 上游无独立标题字段，改用 Agent 完整操作')
    if any(x['kind'] not in {'image','video'} for x in pack['assets']): raise PublishError('上游无法承接全部素材')
    if pack['content_type']=='video' and any(x['kind']!='video' for x in pack['assets']): raise PublishError('上游视频预填不能保留混合图片')
    if pack['content_type']!='video' and any(x['kind']!='image' for x in pack['assets']): raise PublishError('上游普通帖只能承接图片')
    repo=Path(cfg.get('repository','')).expanduser()
    runtime=Path(cfg.get('runtime','')).expanduser()
    if not repo.is_absolute() or not runtime.is_absolute() or not runtime.is_file() or not os.access(runtime,os.X_OK): raise PublishError('上游仓和 bun 需要已安装的绝对路径')
    def git(*args):
        result=subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,check=False)
        if result.returncode: raise PublishError('上游 Git 版本检查失败')
        return result.stdout.strip()
    if Path(git('rev-parse','--show-toplevel')).resolve()!=repo.resolve() or git('rev-parse','HEAD')!=BAOYU_COMMIT or git('status','--porcelain','--untracked-files=all'):
        raise PublishError('只接受锁定 commit 的完整干净上游仓，不执行改版脚本')
    script=(repo/BAOYU_SCRIPTS[pack['content_type']]).resolve()
    if not script.is_relative_to(repo.resolve()) or not script.is_file(): raise PublishError('上游脚本缺失或路径越界')
    command=[str(runtime),str(script)]
    if body: command.append(body)
    for asset in pack['assets']: command.extend(['--video' if asset['kind']=='video' else '--image',asset['absolute_path']])
    _,browser=_account(profile,{'account':pack['account'],'platform':'x'})
    command.extend(['--profile',browser])
    if '--submit' in command: raise PublishError('预填桥禁止 submit')
    return command



def preflight_social(manifest,target,profile,argv):
    """Local-only prerequisites before the durable submission window opens."""
    _account(profile,target)
    pack=build_pack(manifest,target['id'],profile)
    if '--profile' not in argv or argv.index('--profile')+1>=len(argv):
        raise PublishError('社交适配器需要 --profile')
    try: adapter_profile=load_profile(argv[argv.index('--profile')+1])
    except ValueError as exc: raise PublishError('适配器配置无法解析') from exc
    alias=target['account'];platform=target['platform']
    if (adapter_profile.get('accounts',{}).get(alias)!=profile.get('accounts',{}).get(alias)
        or adapter_profile.get('browser_profile')!=profile.get('browser_profile')
        or adapter_profile.get('platform_policies',{}).get(platform,{})!=profile.get('platform_policies',{}).get(platform,{})
        or adapter_profile.get('adapters',{}).get(platform,{}).get('upstream_prefill')!=profile.get('adapters',{}).get(platform,{}).get('upstream_prefill')):
        raise PublishError('适配器账号、浏览器或平台配置与调度器不一致')
    if profile.get('adapters',{}).get(platform,{}).get('upstream_prefill'):
        pinned_prefill_command(pack,profile)
    return dict(local_only=True,target_id=target['id'],platform=platform)


def dispatch(request_path,action,profile,*,timeout=1800):
    if action not in {'execute','verify'}: raise PublishError('非法适配阶段')
    request=read_json(request_path); manifest=read_json(request['manifest_path'])
    if digest(manifest)!=request['manifest_digest']: raise PublishError('交接快照已改变')
    target=request['target']; actual=next((x for x in manifest.get('targets',[]) if x['id']==target['id']),None)
    if actual!=target: raise PublishError('请求目标与封存清单不同')
    identity,browser=_account(profile,target,readonly=action=='verify')
    pack=build_pack(manifest,target['id'],profile,check_files=action=='execute')
    work=dict(schema_version=1,execution_mode='agent-browser',stage=action,pack=pack,pack_digest=digest(pack),
              expected_account_identity=identity,browser_profile=browser,
              reply_requirements=['observed_account_identity','verified_pack_digest (完成读回时)','verified_asset_ids (完成读回时)'])
    previous=read_json(work_path(request_path)) if work_path(request_path).exists() else {}
    if action=='verify' and not previous:
        raise PublishError('缺少首次执行的账号绑定，不能自动认领当前账号；先恢复原执行证据')
    if previous:
        prior_pack=previous.get('pack',{})
        if (previous.get('expected_account_identity')!=identity or previous.get('browser_profile')!=browser
            or previous.get('pack_digest')!=digest(prior_pack)
            or any(prior_pack.get(key)!=pack[key] for key in ('manifest_digest','target_id','platform','account'))):
            raise PublishError('原执行账号、专用 profile 或工作单身份不符；禁止切换账号恢复')
        if action=='verify':
            pack=prior_pack
            work['pack']=pack;work['pack_digest']=digest(pack)
    # A second execute after an uncertain helper attempt must go through verify.
    if action=='execute' and previous.get('upstream_started'): raise PublishError('上游预填已尝试；先只读恢复，不重复执行')
    cfg=profile.get('adapters',{}).get(target['platform'],{}).get('upstream_prefill')
    if action=='execute' and cfg:
        command=pinned_prefill_command(pack,profile)
        work['stage']='authorize-upstream-prefill'
        work['execution_mode']='agent-account-check-then-pinned-baoyu-prefill'
        work['upstream_commit']=BAOYU_COMMIT
        work['instructions']='先用宿主许可工具核对实际账号与专用 profile。确认本轮允许 CDP 脚本，才回应 upstream_prefill_ready=true、browser_mode_authorized=true、observed_browser_profile；无需伪造 remote_id。此时不能点击发送。'
        write_json(work_path(request_path),work)
        observed=agent_adapter.dispatch(request_path,'execute',timeout=timeout)
        validate_observation(observed,pack,identity,'execute')
        if observed.get('upstream_prefill_ready') is not True or observed.get('browser_mode_authorized') is not True or observed.get('observed_browser_profile')!=browser:
            raise PublishError('未获得当前阶段账号、专用浏览器与允许通道的实际核验')
        try: fd=int(os.environ['SANDY_PUBLISH_LOCK_FD']); os.fstat(fd)
        except (KeyError,ValueError,OSError) as exc: raise PublishError('上游必须继承当前发布账号锁') from exc
        work['upstream_started']=True; work['stage']='upstream-running';write_json(work_path(request_path),work)
        try:
            result=subprocess.run(command,capture_output=True,timeout=timeout,pass_fds=(fd,),check=False)
        except (OSError,subprocess.TimeoutExpired) as exc:
            raise PublishError('上游预填结果未知；只读核对原编辑稿，不重复上传') from exc
        work['upstream_returncode']=result.returncode
        work['stage']='observe-upstream-prefill';write_json(work_path(request_path),work)
        if result.returncode: raise PublishError('上游预填非零退出；先核对现有编辑稿，不重复上传')
        # A fresh nonce is generated here. Exit 0 is not a platform receipt.
        work['instructions']='重新观察已预填编辑稿，核对全部字段与媒体，取得真实可恢复编辑身份；只返回 pending，由下一阶段独立读回。'
    else:
        work['upstream_started']=previous.get('upstream_started',False)
    write_json(work_path(request_path),work)
    observed=agent_adapter.dispatch(request_path,action,timeout=timeout)
    return validate_observation(observed,pack,identity,action)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['execute','verify','inspect']);p.add_argument('--request',required=True);p.add_argument('--profile');p.add_argument('--timeout',type=float,default=1800)
    args=p.parse_args()
    try:
        if args.action=='inspect':
            result=dict(task=agent_adapter.inspect(args.request),work_order=read_json(work_path(args.request)))
        else:
            if not args.profile: raise PublishError('需要 --profile')
            if 'SANDY_PUBLISH_LOCK_FD' not in os.environ: raise PublishError('只能由持锁发布运行时调用')
            os.fstat(int(os.environ['SANDY_PUBLISH_LOCK_FD']))
            result=dispatch(args.request,args.action,load_profile(args.profile),timeout=args.timeout)
        print(json.dumps(result,ensure_ascii=False));return 0
    except (PublishError,OSError,ValueError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2
if __name__=='__main__': raise SystemExit(main())
