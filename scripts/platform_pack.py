#!/usr/bin/env python3
"""Build lossless platform work orders; never rewrite content or contact a platform."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from environment import load_profile
from publish_core import PublishError, digest, read_json, resolve_file, target_of, validate, write_json

SOCIAL = {'x', 'weibo', 'douyin', 'bilibili', 'tencent_video', 'xiaohongshu'}
CHECKS = {
 'x': ['区分普通帖与长文章；核实账号当前文字及视频权限', '重新打开真实状态页或可恢复编辑稿，核对文字、链接、媒体与播放'],
 'weibo': ['保留话题和链接；预填不等于公开发表', '从本人内容管理重新打开对应内容，审核中保持 pending'],
 'douyin': ['核对封面、话题、描述及平台要求的真实声明', '从作品管理核对身份和审核状态；投稿成功不等于公开可看'],
 'bilibili': ['核对分区、自制或转载来源、封面、标签及字幕', '保存稿件身份，审核通过后独立打开永久页核对播放'],
 'tencent_video': ['进入腾讯视频创作平台，核对入驻身份、分类与权限；不是视频号', '创作管理与实际 v.qq.com 内容页交叉核对'],
 'xiaohongshu': ['受限账号不进入浏览器；核对图序、封面和话题；最终按钮由作者操作', '仅报告可恢复的草稿或预填，不将当前页面当作永久身份'],
}


def _text(value, label):
    if not isinstance(value, str): raise PublishError(f'{label} 必须为字符串')
    return value


def build_pack(manifest, target_id, profile=None, *, check_files=True):
    """check_files=False is only for sealed resume snapshots, not first execution."""
    if check_files: validate(manifest)
    if not isinstance(manifest, dict) or not manifest.get('targets'): raise PublishError('需要非空交接包')
    target = target_of(manifest, target_id)
    platform = target['platform']
    if platform not in SOCIAL: raise PublishError('公众号请使用专用适配器')
    if target['endpoint'] not in {'draft', 'prefill', 'publish'}: raise PublishError('社交平台终点不支持')
    if platform == 'xiaohongshu' and target['endpoint'] == 'publish': raise PublishError('小红书最终点击须由作者完成')
    fields = {name: _text(target.get(name, ''), name) for name in ('title', 'text', 'description')}
    tags = target.get('tags', [])
    if not isinstance(tags, list) or any(not isinstance(x,str) or not x.strip() for x in tags): raise PublishError('tags 必须为非空字符串数组')
    links = target.get('links', [])
    if not isinstance(links, list): raise PublishError('links 必须为数组')
    for link in links:
        if not isinstance(link, dict) or set(link) - {'label','url'}: raise PublishError('链接使用 {label,url}')
        _text(link.get('label', ''), 'link.label')
        url=link.get('url'); parts=urlsplit(url) if isinstance(url,str) else None
        if not parts or parts.scheme not in {'http','https'} or not parts.netloc or parts.username or parts.password: raise PublishError('链接必须为无凭证的 HTTP(S) 地址')
    asset_index={x['id']:x for x in manifest.get('assets',[])}
    selected=[]
    for ident in target.get('asset_ids',[]):
        if ident not in asset_index: raise PublishError('目标素材不存在')
        asset=dict(asset_index[ident])
        if check_files: path=resolve_file(Path(manifest['source_root']),asset['path'])
        else: path=(Path(manifest['source_root'])/asset['path']).resolve()
        asset['absolute_path']=str(path); selected.append(asset)
    subtitles=target.get('subtitle_asset_ids', [x['id'] for x in selected if x['kind']=='subtitle'])
    if not isinstance(subtitles,list) or any(not isinstance(x,str) for x in subtitles) or len(set(subtitles))!=len(subtitles) or any(x not in target.get('asset_ids',[]) or asset_index[x]['kind']!='subtitle' for x in subtitles): raise PublishError('字幕必须唯一引用目标中的 subtitle 素材')
    if set(subtitles)!={x['id'] for x in selected if x['kind']=='subtitle'}: raise PublishError('目标字幕不能静默遗漏')
    cover=target.get('cover_asset_id')
    if cover and (cover not in target.get('asset_ids',[]) or asset_index[cover]['kind']!='image'): raise PublishError('封面必须引用目标图片')
    metadata=target.get('platform_metadata',{})
    if not isinstance(metadata,dict): raise PublishError('platform_metadata 必须为对象')
    body=fields['text'] or fields['description']
    tag_text=' '.join(('#'+x.strip('#')+'#') if platform=='weibo' else ('#'+x.lstrip('#')) for x in tags)
    link_text='\n'.join((x.get('label','')+'：' if x.get('label') else '')+x['url'] for x in links)
    composed='\n\n'.join(x for x in (body,tag_text,link_text) if x)
    pack=dict(schema_version=1,manifest_digest=digest(manifest),target_id=target_id,platform=platform,
              account=target['account'],content_type=target['content_type'],endpoint=target['endpoint'],
              fields={**fields,'tags':tags,'links':links,'composed_text':composed},assets=selected,
              cover_asset_id=cover,subtitle_asset_ids=subtitles,platform_metadata=metadata,
              document=next((dict(x) for x in manifest.get('documents',[]) if x['id']==target.get('document_id')),None),
              original_target=dict(target),checks=list(CHECKS[platform]),warnings=[])
    policies=(profile or {}).get('platform_policies',{})
    if not isinstance(policies,dict): raise PublishError('platform_policies 必须为对象')
    policy=policies.get(platform,{})
    if not isinstance(policy,dict): raise PublishError('平台规则必须为对象')
    allowed={'required_fields','max_lengths','max_assets','allowed_extensions','require_cover','require_subtitles'}
    if set(policy)-allowed: raise PublishError('未知平台规则；拒绝忽略拼写错误')
    required=policy.get('required_fields',[])
    if not isinstance(required,list) or any(x not in {'title','text','description','composed_text','tags','links'} for x in required): raise PublishError('required_fields 无效')
    for name in required:
        if not pack['fields'][name]: raise PublishError(f'平台要求字段：{name}')
    lengths=policy.get('max_lengths',{})
    if not isinstance(lengths,dict): raise PublishError('max_lengths 必须为对象')
    for name,limit in lengths.items():
        if name not in {'title','text','description','composed_text'} or type(limit)!=int or limit<1: raise PublishError('max_lengths 必须为有效字段及正整数')
        if len(pack['fields'][name])>limit: raise PublishError(f'{name} 超过已配置限制；交回生产端修改，不截断')
    caps=policy.get('max_assets',{})
    if not isinstance(caps,dict): raise PublishError('max_assets 必须为对象')
    for kind,limit in caps.items():
        if kind not in {'image','video','audio','subtitle'} or type(limit)!=int or limit<0: raise PublishError('max_assets 配置无效')
        if sum(x['kind']==kind for x in selected)>limit: raise PublishError(f'{kind} 数量超过配置')
    ext=policy.get('allowed_extensions',{})
    if not isinstance(ext,dict): raise PublishError('allowed_extensions 必须为对象')
    for kind,values in ext.items():
        if kind not in {'image','video','audio','subtitle'} or not isinstance(values,list) or not values or any(not isinstance(x,str) or not x.startswith('.') for x in values): raise PublishError('allowed_extensions 配置无效')
        if any(x['kind']==kind and Path(x['path']).suffix.lower() not in values for x in selected): raise PublishError(f'{kind} 文件格式不符合配置')
    for key in ('require_cover','require_subtitles'):
        if key in policy and type(policy[key])!=bool: raise PublishError(f'{key} 必须为布尔值')
    if policy.get('require_cover') and not cover: raise PublishError('平台要求封面')
    if policy.get('require_subtitles') and not subtitles: raise PublishError('平台要求字幕')
    if not policy: pack['warnings'].append('未配置账号实测规格；执行前由 Agent 核对当前页面限制，不预设通用上限')
    if fields['text'] and fields['description'] and fields['text']!=fields['description']:
        pack['warnings'].append('text 与 description 不同：各自保留，执行时按平台字段映射，禁止丢弃描述')
    if subtitles: pack['checks'].append('全部字幕必须独立确认上传或明确退回生产端；不得声称无字幕的成片已包含字幕')
    return pack


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('manifest');p.add_argument('--target',required=True);p.add_argument('--profile');p.add_argument('--output')
    args=p.parse_args()
    try:
        result=build_pack(read_json(args.manifest),args.target,load_profile(args.profile) if args.profile else {})
        if args.output: write_json(args.output,result)
        print(json.dumps(result,ensure_ascii=False)); return 0
    except (PublishError,OSError,ValueError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False)); return 2
if __name__=='__main__': raise SystemExit(main())
