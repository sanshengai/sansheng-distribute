#!/usr/bin/env python3
"""CLI entry. No operation implicitly escalates from a draft to publication."""
import argparse
import json
from pathlib import Path
from publish_core import PublishError, Store, read_json, write_json, run, seal, validate, preflight
from environment import resolve_paths, load_profile, EnvironmentError


def main():
    parser = argparse.ArgumentParser(description="统一发布：交接检查、版本授权、持久执行与读回")
    parser.add_argument("--state-dir")
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("seal", help="锁定源文件哈希及已指定的媒体位置；不代表作者授权")
    p.add_argument("spec"); p.add_argument("--output", required=True)
    p = sub.add_parser("check"); p.add_argument("manifest")
    p = sub.add_parser('media-inspect', help='只读检查封存视频的实际尺寸、时长和大小；不连接平台')
    p.add_argument('manifest'); p.add_argument('--target', required=True)
    p = sub.add_parser('preflight', help='仅做本地依赖、账号配置与素材检查，不请求 token、不上传')
    p.add_argument('manifest'); p.add_argument('--target', required=True)
    p = sub.add_parser("status"); p.add_argument("--key")
    p = sub.add_parser('metrics', help='只读查询同一账本的执行/核验耗时；不重跑或上传')
    p.add_argument('--key', required=True)
    p = sub.add_parser("return-write", help="只读重验既有草稿并生成 Write 回传凭证；不上传或新建")
    p.add_argument("key"); p.add_argument("--article-dir", required=True); p.add_argument("--write-scripts", required=True)
    p = sub.add_parser("continue-youtube", help="按原授权继续 YouTube 原上传会话；会写入平台，不创建新会话")
    p.add_argument("key")
    p = sub.add_parser('youtube-bundle-prepare', help='封存 YouTube 视频与所需封面、合集的组合任务')
    p.add_argument('manifest'); p.add_argument('--target', required=True)
    for name in ('youtube-bundle-status', 'youtube-bundle-run', 'youtube-bundle-continue'):
        p = sub.add_parser(name); p.add_argument('key')
    p = sub.add_parser('youtube-component-prepare', help='为已核验的原视频准备独立封面或合集任务；不连接平台')
    p.add_argument('key'); p.add_argument('--spec', required=True)
    p = sub.add_parser('youtube-component-authorize', help='记录已有用户对具体组件任务的授权')
    p.add_argument('key'); p.add_argument('--statement', required=True)
    for name in ('youtube-component-status', 'youtube-component-revoke'):
        p = sub.add_parser(name); p.add_argument('key')
    for name in ('youtube-component-run', 'youtube-component-verify'):
        p = sub.add_parser(name); p.add_argument('key'); p.add_argument('--part', choices=('cover', 'playlist'), required=True)
    p = sub.add_parser('youtube-cover-review', help='记录观察者对平台读回封面的视觉复核；不发送平台请求')
    p.add_argument('key'); p.add_argument('--parent-key', required=True); p.add_argument('--video-id', required=True)
    p.add_argument('--cover-sha256', required=True); p.add_argument('--thumbnail-url', required=True)
    p.add_argument('--thumbnail-path', required=True); p.add_argument('--reviewer', choices=('user', 'agent'), required=True)
    p.add_argument('--statement', required=True); p.add_argument('--reviewed-at', required=True)
    p = sub.add_parser("resume-job", help="源文件或交接清单丢失时，按账本 key 只读恢复")
    p.add_argument("key")
    for name in ('recover-wechat','recover-x','recover-weibo'):
        p = sub.add_parser(name, help='只读核对明确候选身份后恢复未知任务，不重新提交')
        p.add_argument('key'); p.add_argument('--remote-id', required=True)
    for name in ("prepare", "authorize", "run", "resume", "advance", "adopt"):
        p = sub.add_parser(name); p.add_argument("manifest"); p.add_argument("--target", required=True)
        if name == "authorize":
            p.add_argument("--statement", required=True, help="已有用户授权的实际原话，不得编造")
    p = sub.add_parser("pack", help="生成平台发布包；不截断文案、不执行远端操作")
    p.add_argument("manifest"); p.add_argument("--target", required=True); p.add_argument("--output", required=True)
    p = sub.add_parser("platform-guide", help="只读查看平台交接类型、本地规则和配置；不连接平台")
    p.add_argument("platform")
    p = sub.add_parser("batch-prepare"); p.add_argument("manifest"); p.add_argument("--target", action="append")
    p = sub.add_parser("batch-preflight", help="一次汇总所选目标的本地阻塞；不建任务、不授权、不连接平台")
    p.add_argument("manifest"); p.add_argument("--target", action="append")
    p = sub.add_parser("batch-report", help="将已有批次的账本快照生成本地 HTML；不联网、不覆盖已有文件")
    p.add_argument("campaign_id", nargs="+"); p.add_argument("--output", required=True)
    p = sub.add_parser("series-report", help="按平台和集数汇总账本快照；不联网、不覆盖已有文件")
    p.add_argument("spec"); p.add_argument("--output", required=True)
    for name in ("batch-authorize", "batch-run", "batch-resume", "batch-status"):
        p = sub.add_parser(name); p.add_argument("campaign_id")
        if name != "batch-status": p.add_argument("--target", action="append")
        if name == "batch-authorize": p.add_argument("--statement", required=True)
        if name in {"batch-run", "batch-resume"}: p.add_argument("--max-parallel", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    store = None
    try:
        if args.action not in {"seal", "check", "media-inspect"}:
            # This read-only query never opens a ledger; don't require choosing
            # between legacy and new state directories merely to view guidance.
            state_hint = (args.state_dir or ".") if args.action == "platform-guide" else args.state_dir
            paths = resolve_paths(args.profile, state_hint)
            if (args.action == "platform-guide" and paths['profile_source'] != 'default'
                    and not paths['profile_exists']):
                raise EnvironmentError('明确指定的配置文件不存在或不是普通文件')
            args.profile, args.state_dir = paths["profile"], paths["state_dir"]
        if args.action == "seal":
            value = seal(args.spec, args.output)
            result = {"manifest": str(Path(args.output).resolve()), **validate(value)}
        elif args.action == "check":
            result = validate(read_json(args.manifest))
        elif args.action == 'media-inspect':
            from media_inspect import inspect
            result = inspect(read_json(args.manifest), args.target)
        elif args.action == 'preflight':
            result = preflight(read_json(args.manifest), args.target, load_profile(args.profile))
        elif args.action == 'batch-preflight':
            from campaign import preflight_campaign
            result = preflight_campaign(args.manifest, load_profile(args.profile), args.target)
        elif args.action == "pack":
            from platform_pack import build_pack
            profile = load_profile(args.profile) if Path(args.profile).is_file() else {}
            result = build_pack(read_json(args.manifest), args.target, profile)
            write_json(args.output, result)
        elif args.action == "platform-guide":
            from platform_guide import platform_guide
            profile = load_profile(args.profile) if Path(args.profile).is_file() else {}
            result = platform_guide(args.platform, profile)
        else:
            store = Store(args.state_dir)
            if args.action.startswith("batch-"):
                from campaign import prepare_campaign, authorize_campaign, run_campaign, campaign_status
                if args.action == "batch-prepare":
                    result = prepare_campaign(args.manifest, store, args.target)
                elif args.action == "batch-authorize":
                    result = authorize_campaign(args.campaign_id, store, args.statement, args.target)
                elif args.action == "batch-status":
                    result = campaign_status(args.campaign_id, store)
                elif args.action == "batch-report":
                    from campaign_report import write_report
                    result = write_report(args.campaign_id, store, args.output)
                else:
                    result = run_campaign(args.campaign_id, load_profile(args.profile), store, args.target,
                                          resume=args.action == "batch-resume", max_parallel=args.max_parallel)
            elif args.action == "series-report":
                from series_report import write_series_report
                result = write_series_report(args.spec, store, args.output)
            elif args.action == "status":
                result = store.status(args.key)
            elif args.action == 'metrics':
                result = store.metrics(args.key)
            elif args.action == 'youtube-bundle-prepare':
                result = store.prepare_youtube_bundle(read_json(args.manifest), args.target, load_profile(args.profile))
            elif args.action == 'youtube-bundle-status':
                result = store.youtube_bundle_status(args.key)
            elif args.action in {'youtube-bundle-run', 'youtube-bundle-continue'}:
                from publish_core import run_youtube_bundle
                result = run_youtube_bundle(store, args.key, load_profile(args.profile),
                                            continue_upload=args.action == 'youtube-bundle-continue')
            elif args.action == 'youtube-component-prepare':
                result = store.prepare_youtube_component(args.key, read_json(args.spec), load_profile(args.profile))
            elif args.action == 'youtube-component-authorize':
                result = store.authorize_youtube_component(args.key, args.statement)
            elif args.action == 'youtube-component-status':
                result = store.youtube_component_status(args.key)
            elif args.action == 'youtube-component-revoke':
                result = store.revoke_youtube_component(args.key)
            elif args.action in {'youtube-component-run', 'youtube-component-verify'}:
                from publish_core import run_youtube_component
                result = run_youtube_component(store, args.key, args.part, load_profile(args.profile),
                                               verify_only=args.action == 'youtube-component-verify')
            elif args.action == 'youtube-cover-review':
                result = store.record_youtube_cover_review(args.parent_key, args.video_id, args.cover_sha256,
                    args.thumbnail_url, args.thumbnail_path, args.reviewer, args.statement, args.reviewed_at,
                    component_key=args.key)
            elif args.action == "return-write":
                from write_bridge import return_to_write
                result = return_to_write(store,args.key,load_profile(args.profile),args.article_dir,args.write_scripts)
            elif args.action in {"resume-job", "recover-wechat", "recover-x", "recover-weibo"}:
                job = store.status(args.key)
                result = run(store.directory / "snapshots" / f"{args.key}.json", job["target_id"], load_profile(args.profile), store,
                             resume=True, recover_remote_id=args.remote_id if args.action.startswith('recover-') else None,
                             recovery_platform=args.action.removeprefix('recover-') if args.action.startswith('recover-') else 'wechat')
            elif args.action == "continue-youtube":
                job = store.status(args.key)
                path = store.directory / 'snapshots' / f"{job['key']}.json"
                result = run(path, job['target_id'], load_profile(args.profile), store,
                             resume=True, youtube_continue=True)
            elif args.action in {"run", "resume", "advance", "adopt"}:
                result = run(args.manifest, args.target, load_profile(args.profile), store,
                             resume=args.action == "resume",advance=args.action == "advance",adopt=args.action == "adopt")
            elif args.action == "authorize":
                result = store.authorize(read_json(args.manifest), args.target, args.statement)
            else:
                result = store.prepare(read_json(args.manifest), args.target)
        ok = args.action != 'batch-preflight' or result['all_local_checks_passed']
        print(json.dumps({"ok": ok, "result": result}, ensure_ascii=False, indent=2))
        return 0 if ok else 2
    except (PublishError, EnvironmentError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
