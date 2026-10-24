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
    p = sub.add_parser('preflight', help='仅做本地依赖、账号配置与素材检查，不请求 token、不上传')
    p.add_argument('manifest'); p.add_argument('--target', required=True)
    p = sub.add_parser("status"); p.add_argument("--key")
    p = sub.add_parser("return-write", help="只读重验既有草稿并生成 Write 回传凭证；不上传或新建")
    p.add_argument("key"); p.add_argument("--article-dir", required=True); p.add_argument("--write-scripts", required=True)
    p = sub.add_parser("resume-job", help="源文件或交接清单丢失时，按账本 key 只读恢复")
    p.add_argument("key")
    p = sub.add_parser('recover-wechat', help='官方只读核对候选草稿后绑定未知任务，不新建稿')
    p.add_argument('key'); p.add_argument('--remote-id', required=True)
    for name in ("prepare", "authorize", "run", "resume", "advance", "adopt"):
        p = sub.add_parser(name); p.add_argument("manifest"); p.add_argument("--target", required=True)
        if name == "authorize":
            p.add_argument("--statement", required=True, help="已有用户授权的实际原话，不得编造")
    p = sub.add_parser("pack", help="生成平台发布包；不截断文案、不执行远端操作")
    p.add_argument("manifest"); p.add_argument("--target", required=True); p.add_argument("--output", required=True)
    p = sub.add_parser("batch-prepare"); p.add_argument("manifest"); p.add_argument("--target", action="append")
    for name in ("batch-authorize", "batch-run", "batch-resume", "batch-status"):
        p = sub.add_parser(name); p.add_argument("campaign_id")
        if name != "batch-status": p.add_argument("--target", action="append")
        if name == "batch-authorize": p.add_argument("--statement", required=True)
    args = parser.parse_args()
    store = None
    try:
        if args.action not in {"seal", "check"}:
            paths = resolve_paths(args.profile, args.state_dir)
            args.profile, args.state_dir = paths["profile"], paths["state_dir"]
        if args.action == "seal":
            value = seal(args.spec, args.output)
            result = {"manifest": str(Path(args.output).resolve()), **validate(value)}
        elif args.action == "check":
            result = validate(read_json(args.manifest))
        elif args.action == 'preflight':
            result = preflight(read_json(args.manifest), args.target, load_profile(args.profile))
        elif args.action == "pack":
            from platform_pack import build_pack
            profile = load_profile(args.profile) if Path(args.profile).is_file() else {}
            result = build_pack(read_json(args.manifest), args.target, profile)
            write_json(args.output, result)
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
                else:
                    result = run_campaign(args.campaign_id, load_profile(args.profile), store, args.target,
                                          resume=args.action == "batch-resume")
            elif args.action == "status":
                result = store.status(args.key)
            elif args.action == "return-write":
                from write_bridge import return_to_write
                result = return_to_write(store,args.key,load_profile(args.profile),args.article_dir,args.write_scripts)
            elif args.action in {"resume-job", "recover-wechat"}:
                job = store.status(args.key)
                result = run(store.directory / "snapshots" / f"{args.key}.json", job["target_id"], load_profile(args.profile), store,
                             resume=True, recover_remote_id=args.remote_id if args.action == 'recover-wechat' else None)
            elif args.action in {"run", "resume", "advance", "adopt"}:
                result = run(args.manifest, args.target, load_profile(args.profile), store,
                             resume=args.action == "resume",advance=args.action == "advance",adopt=args.action == "adopt")
            elif args.action == "authorize":
                result = store.authorize(read_json(args.manifest), args.target, args.statement)
            else:
                result = store.prepare(read_json(args.manifest), args.target)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except (PublishError, EnvironmentError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
