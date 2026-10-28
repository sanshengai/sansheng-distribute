#!/usr/bin/env python3
"""YouTube Data API resumable-upload candidate; disabled until independently accepted.

A resumable URI is secret: it is held only in a 0600 private receipt, never
returned to the core ledger. An interrupted creation is never re-initiated.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from environment import load_profile
from platform_pack import build_pack
from publish_core import PublishError, digest, file_hash, read_json, write_json

API = 'https://www.googleapis.com'
TOKEN_ENV = re.compile(r'^[A-Z][A-Z0-9_]{1,79}$')
VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
CHANNEL_ID = re.compile(r'^UC[A-Za-z0-9_-]{22}$')
CHUNK = 4 * 1024 * 1024  # Multiple of 256 KiB; final chunk may be smaller.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class APIClient:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def public_video(self, video_id):
        url = 'https://www.youtube.com/oembed?' + urllib.parse.urlencode({'url':'https://www.youtube.com/watch?v='+video_id, 'format':'json'})
        try:
            with self.opener.open(urllib.request.Request(url), timeout=30) as response:
                if response.status != 200: return None
                return json_body(response.read(1024*1024+1))
        except (OSError, ValueError, PublishError):
            return None

    def granted_scopes(self):
        request = urllib.request.Request('https://oauth2.googleapis.com/tokeninfo', method='POST',
            data=urllib.parse.urlencode({'access_token':self.token}).encode(),
            headers={'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status != 200: raise PublishError('授权范围无法核验')
                scope = json_body(response.read(1024*1024+1)).get('scope')
        except (OSError, ValueError, PublishError):
            raise PublishError('授权范围无法核验') from None
        if not isinstance(scope, str): raise PublishError('授权范围无法核验')
        return set(scope.split())

    def thumbnail(self, url, video_id):
        parsed = urllib.parse.urlsplit(url if isinstance(url, str) else '')
        parts = parsed.path.strip('/').split('/')
        if (parsed.scheme != 'https' or parsed.hostname != 'i.ytimg.com' or parsed.username or parsed.password
            or parsed.port not in (None,443) or parsed.fragment or len(parts) != 3
            or parts[:2] not in (['vi',video_id], ['vi_webp',video_id])
            or parts[2] not in {'default.jpg','maxresdefault.jpg','hqdefault.jpg','mqdefault.jpg','sddefault.jpg','fhddefault.jpg','default.webp','maxresdefault.webp','hqdefault.webp','mqdefault.webp','sddefault.webp'}):
            raise PublishError('YouTube 封面地址不可信')
        try:
            with self.opener.open(urllib.request.Request(url), timeout=30) as response:
                if response.status != 200: return None
                data = response.read(16*1024*1024+1)
            return data if 0 < len(data) <= 16*1024*1024 else None
        except (OSError, ValueError):
            return None

    def call(self, method, url, *, headers=None, data=None):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname not in {'www.googleapis.com', 'youtube.googleapis.com'}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise PublishError('YouTube API 地址不可信')
        req = urllib.request.Request(url, data=data, method=method,
            headers={'Authorization': 'Bearer '+self.token, **(headers or {})})
        try:
            with self.opener.open(req, timeout=120) as response:
                return response.status, dict(response.headers), response.read(1024*1024+1)
        except urllib.error.HTTPError as exc:
            if exc.code == 308:
                return 308, dict(exc.headers), exc.read(1024*1024+1)
            # Never echo URL, body, headers or original exception: they may be sensitive.
            raise PublishError('YouTube 请求未完成；先查询同一任务') from None
        except (OSError, ValueError):
            raise PublishError('YouTube 请求中断；先查询同一任务') from None


def json_body(body):
    if len(body) > 1024*1024:
        raise PublishError('YouTube 响应超出上限')
    try: value = json.loads(body)
    except (ValueError, UnicodeError): raise PublishError('YouTube 响应格式不符') from None
    if not isinstance(value, dict): raise PublishError('YouTube 响应格式不符')
    return value


def tags_equal(actual, expected):
    """YouTube can reorder tags; every declared tag and count must still match."""
    return (isinstance(actual, list) and isinstance(expected, list)
            and all(isinstance(tag, str) for tag in actual + expected)
            and Counter(actual) == Counter(expected))


def header(headers, key):
    return next((v for k, v in headers.items() if k.lower() == key.lower()), None)


def session_url(value):
    if not isinstance(value, str): raise PublishError('YouTube 未返回上传会话')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in {'www.googleapis.com', 'youtube.googleapis.com'}
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not parsed.path.startswith('/upload/youtube/v3/videos') or not parsed.query or parsed.fragment):
        raise PublishError('YouTube 上传会话地址不可信')
    return value


def refresh_access_token(refresh_token, client_id, client_secret):
    """Exchange only at Google's fixed endpoint; no credentials are persisted or logged."""
    url='https://oauth2.googleapis.com/token'
    body=urllib.parse.urlencode({'grant_type':'refresh_token', 'refresh_token':refresh_token,
                                'client_id':client_id, 'client_secret':client_secret}).encode('utf-8')
    request=urllib.request.Request(url, method='POST', data=body,
                                   headers={'Content-Type':'application/x-www-form-urlencoded'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200: raise PublishError('YouTube OAuth 刷新未成功')
            result=json_body(response.read(1024*1024+1))
    except (OSError, ValueError, PublishError):
        raise PublishError('YouTube OAuth 刷新未成功；检查账号授权') from None
    token=result.get('access_token')
    if (not isinstance(token,str) or not token or any(c in token for c in '\r\n')
            or not isinstance(result.get('token_type'),str) or result['token_type'].lower() != 'bearer'):
        raise PublishError('YouTube OAuth 刷新响应无效')
    return token


def config(profile, target, *, writing=True, resolve_token=True):
    account = profile.get('accounts', {}).get(target['account'], {})
    if target['platform'] != 'youtube' or 'youtube' not in account.get('platforms', []):
        raise PublishError('YouTube 账号或平台绑定不符')
    channel = account.get('public_identities', {}).get('youtube')
    if not isinstance(channel, str) or not CHANNEL_ID.fullmatch(channel):
        raise PublishError('YouTube 须绑定已确认的频道 ID')
    if writing and account.get('restricted'):
        raise PublishError('账号受限，仅允许只读核验')
    refresh_names = [account.get('youtube_refresh_token_env'), account.get('youtube_client_id_env'),
                     account.get('youtube_client_secret_env')]
    access_name = account.get('youtube_token_env')
    if access_name and any(refresh_names):
        raise PublishError('YouTube access 与 refresh 凭据模式不能混用')
    names = [access_name] if access_name else refresh_names
    if any(not isinstance(name, str) or not TOKEN_ENV.fullmatch(name) for name in names):
        raise PublishError('YouTube 须配置完整的 OAuth 令牌环境变量名')
    values = [os.environ.get(name, '') for name in names]
    if any(not value or any(c in value for c in '\r\n') for value in values):
        raise PublishError('YouTube OAuth 凭据不可用')
    token = (values[0] if access_name else refresh_access_token(*values) if resolve_token else '')
    return channel, token


def identity(client, expected):
    code, _, body = client.call('GET', API+'/youtube/v3/channels?part=id&mine=true')
    if code != 200: raise PublishError('无法核验 YouTube 授权频道')
    items = json_body(body).get('items')
    if not isinstance(items, list) or len(items) != 1 or items[0].get('id') != expected:
        raise PublishError('YouTube OAuth 频道与目标频道不一致')


def extract_component_spec(pack):
    """Inspect sealed bundle fields without authorizing or writing anything."""
    if not isinstance(pack,dict) or not isinstance(pack.get('platform_metadata'),dict):
        raise PublishError('YouTube 交接包格式无效')
    metadata=pack['platform_metadata']; assets=pack.get('assets')
    if not isinstance(assets,list) or not all(isinstance(a,dict) for a in assets):
        raise PublishError('YouTube 素材格式无效')
    playlist=metadata.get('playlist_id'); position=metadata.get('playlist_position')
    if playlist is not None and (not isinstance(playlist,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',playlist)):
        raise PublishError('YouTube 合集 ID 无效')
    if position is not None and (playlist is None or type(position) is not int or position < 0):
        raise PublishError('YouTube 合集位置无效')
    cover_id=pack.get('cover_asset_id'); cover=None
    if cover_id is not None:
        found=[a for a in assets if a.get('id')==cover_id and a.get('kind')=='image']
        if len(found)!=1: raise PublishError('YouTube 封面引用不唯一')
        item=found[0]
        if (not isinstance(item.get('id'),str) or not item['id']
                or not isinstance(item.get('absolute_path'),str) or not Path(item['absolute_path']).is_absolute()
                or not isinstance(item.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',item['sha256'])):
            raise PublishError('YouTube 封面素材身份无效')
        cover={k:item[k] for k in ('id','absolute_path','sha256')}
    video=[a for a in assets if a.get('kind')=='video']
    if len(video)!=1 or len(assets)!=(1+int(cover is not None)) or pack.get('subtitle_asset_ids'):
        raise PublishError('YouTube 附加素材或字幕不可忽略')
    return {'cover':cover,'playlist_id':playlist,'playlist_position':position}


def make_plan(pack, *, bundle=False):
    if pack['platform'] != 'youtube' or pack['content_type'] != 'video' or pack['endpoint'] != 'publish':
        raise PublishError('YouTube API 候选仅支持公开视频发布')
    fields, meta = pack['fields'], pack['platform_metadata']
    if meta.get('visibility') != 'public': raise PublishError('当前候选只支持明确的 public 终点')
    allowed = {'visibility', 'made_for_kids', 'altered_content', 'category_id'}
    if bundle:
        components=extract_component_spec(pack)
        if not components['cover'] and not components['playlist_id']:
            raise PublishError('组合任务未指定封面或合集')
        allowed |= {'playlist_id','playlist_position'}
    if set(meta) - allowed: raise PublishError('YouTube 字段尚无经过验收的映射，不能忽略')
    if not fields['title'] or (fields['text'] and fields['description'] and fields['text'] != fields['description']):
        raise PublishError('YouTube 标题缺失或正文与简介冲突')
    if not bundle and (pack['cover_asset_id'] or pack['subtitle_asset_ids'] or len(pack['assets']) != 1):
        raise PublishError('YouTube 封面、字幕或附加素材须使用组合任务，不可忽略')
    video = next((a for a in pack['assets'] if a['kind']=='video'),None)
    if video is None: raise PublishError('YouTube 缺视频素材')
    if video['kind'] != 'video': raise PublishError('YouTube 缺视频素材')
    if type(meta.get('made_for_kids')) is not bool:
        raise PublishError('YouTube 受众必须明确')
    body = fields['description'] or fields['text']
    if fields['links']:
        body += ('\n\n' if body else '') + '\n'.join((x['label']+'：' if x.get('label') else '') + x['url'] for x in fields['links'])
    snippet = {'title': fields['title'], 'description': body}
    if fields['tags']: snippet['tags'] = fields['tags']
    category = meta.get('category_id')
    if category is not None:
        if not isinstance(category, str) or not category.isdigit(): raise PublishError('YouTube 分类须为数字 ID')
        snippet['categoryId'] = category
    status = {'privacyStatus':'public', 'selfDeclaredMadeForKids':meta['made_for_kids']}
    if 'altered_content' in meta:
        if type(meta['altered_content']) is not bool: raise PublishError('YouTube 合成媒体声明须为布尔值')
        status['containsSyntheticMedia'] = meta['altered_content']
    return video, {'snippet':snippet, 'status':status}


def preflight_youtube(manifest, target, profile, argv, expected_action=None, *, bundle=False, recovery=False):
    """Local check before the core claims the remote-write window; no network."""
    if type(recovery) is not bool or (recovery and expected_action != 'verify'):
        raise PublishError('只读恢复预检仅允许明确的 verify 动作')
    writing = not recovery and expected_action != 'verify_component'
    channel, _ = config(profile, target, writing=writing, resolve_token=False)
    positions = [i for i, item in enumerate(argv) if Path(item).name == 'youtube_adapter.py']
    if len(positions) != 1 or Path(argv[positions[0]]).resolve() != Path(__file__).resolve():
        raise PublishError('YouTube 适配器脚本必须是已安装的本包入口')
    i = positions[0]
    if i != 1 or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', Path(argv[0]).name):
        raise PublishError('YouTube 适配器必须由明确的 Python 解释器直接运行')
    if i + 1 >= len(argv) or argv[i+1] not in {'execute','verify','continue','component','verify_component'}:
        raise PublishError('YouTube 适配器缺少明确动作')
    if expected_action is not None and argv[i+1] != expected_action:
        raise PublishError('YouTube 适配器动作与调度阶段不一致')
    if argv.count('--profile') != 1 or argv.index('--profile') + 1 >= len(argv):
        raise PublishError('YouTube 适配器需要显式 profile')
    allowed_tail = [argv[i+1], '--profile', argv[argv.index('--profile')+1]]
    if argv[i+1:] != allowed_tail:
        raise PublishError('YouTube 适配器参数无效')
    selected = load_profile(argv[argv.index('--profile')+1])
    selected_channel, _ = config(selected, target, writing=writing, resolve_token=False)
    if selected_channel != channel or selected['accounts'][target['account']] != profile['accounts'][target['account']]:
        raise PublishError('YouTube 适配器配置与调度器账号不一致')
    if argv[i+1] not in {'component', 'verify_component'}:
        pack=build_pack(manifest, target['id'], profile, check_files=not recovery)
        video, _ = make_plan(pack,bundle=bundle)
        if not recovery: check_video(video)
        if bundle and not recovery:
            cover=extract_component_spec(pack)['cover']
            if cover: _cover_bytes(cover)
    return {'local_only':True, 'argv_validated':True}


def checkpoint_path(request_path):
    return Path(request_path).with_suffix('.youtube-private.json')


def read_checkpoint(path, binding):
    if not path.is_file(): raise PublishError('缺少原上传会话；保持结果未知，禁止新建')
    if path.stat().st_mode & 0o077: raise PublishError('YouTube 上传凭据权限不安全')
    cp = read_json(path)
    if cp.get('binding') != binding or cp.get('phase') not in ('session', 'completed'):
        raise PublishError('YouTube 会话尚未确认或与原任务不符；禁止新建')
    session_url(cp.get('session_url'))
    return cp


def check_video(video):
    path = Path(video['absolute_path'])
    if not path.is_file() or file_hash(path) != video['sha256']:
        raise PublishError('YouTube 原视频不存在或内容已变化')
    size = path.stat().st_size
    if size <= 0: raise PublishError('YouTube 视频为空')
    return path, size


def snapshot_path(request_path):
    return Path(request_path).with_suffix('.youtube-media')


def create_snapshot(video, request_path):
    """Freeze the producer bytes before creating any remote upload session."""
    source, _ = check_video(video)
    dest = snapshot_path(request_path)
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        raise PublishError('已有 YouTube 私有素材快照；禁止重新创建') from None
    try:
        import hashlib
        hasher = hashlib.sha256(); size = 0
        with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
            while chunk := incoming.read(1024*1024):
                hasher.update(chunk); size += len(chunk); output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        if size <= 0 or hasher.hexdigest() != video['sha256']:
            raise PublishError('YouTube 复制素材期间原文件变化；未上传')
        dest.chmod(0o400)
        return dest, size
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def check_snapshot(video, request_path, size):
    path = snapshot_path(request_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise PublishError('YouTube 私有视频快照不可用或权限不安全')
    if path.stat().st_size != size or file_hash(path) != video['sha256']:
        raise PublishError('YouTube 私有视频快照已改变；禁止继续')
    return path


def probe(client, cp, size):
    code, headers, body = client.call('PUT', session_url(cp['session_url']),
        headers={'Content-Length':'0', 'Content-Range':f'bytes */{size}'}, data=b'')
    if code in (200, 201):
        video_id = json_body(body).get('id')
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise PublishError('YouTube 完成响应缺少视频 ID')
        return video_id, size
    if code != 308: raise PublishError('YouTube 原上传会话无法确认')
    value = header(headers, 'Range')
    if value is None: return None, 0
    match = re.fullmatch(r'bytes=0-(\d+)', value)
    if not match or int(match.group(1)) >= size: raise PublishError('YouTube 上传偏移无效')
    return None, int(match.group(1))+1


def transfer(client, cp, path, size, offset, checkpoint):
    with path.open('rb') as file:
        file.seek(offset)
        while offset < size:
            data = file.read(min(CHUNK, size-offset))
            if not data: raise PublishError('视频在上传中变化')
            end = offset+len(data)-1
            code, headers, body = client.call('PUT', cp['session_url'], headers={
                'Content-Type':'application/octet-stream', 'Content-Length':str(len(data)),
                'Content-Range':f'bytes {offset}-{end}/{size}'}, data=data)
            if code in (200, 201):
                video_id = json_body(body).get('id')
                if end != size-1 or not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
                    raise PublishError('YouTube 完成响应不符合上传位置')
                cp.update(phase='completed', video_id=video_id)
                write_json(checkpoint, cp)
                return video_id
            if code != 308: raise PublishError('YouTube 上传响应未确认')
            value = header(headers, 'Range')
            match = re.fullmatch(r'bytes=0-(\d+)', value or '')
            if not match or int(match.group(1)) != end: raise PublishError('YouTube 已接收偏移异常；先查询')
            offset = end+1
    raise PublishError('YouTube 未返回完成回执；先查询')


def _component_binding(request, channel):
    component = request.get('component_job')
    fields = {'component_key','original_job_key','video_id','channel','cover','playlist_id',
              'playlist_position','component_digest','part'}
    if not isinstance(component, dict) or set(component) != fields:
        raise PublishError('YouTube 组件任务无效')
    binding = {k:component[k] for k in ('original_job_key','video_id','channel','cover','playlist_id','playlist_position')}
    key, video_id = digest(binding), component['video_id']
    if (component['component_key'] != key or component['component_digest'] != key
            or not isinstance(video_id,str) or not VIDEO_ID.fullmatch(video_id)
            or component['channel'] != channel or component['part'] not in {'cover','playlist'}
            or request.get('job',{}).get('key') != component['original_job_key']
            or request.get('job',{}).get('remote_id') != video_id):
        raise PublishError('YouTube 组件和原视频绑定不符')
    cover, playlist, pos = component['cover'], component['playlist_id'], component['playlist_position']
    if cover is not None and (not isinstance(cover,dict) or set(cover) != {'id','absolute_path','sha256'}
            or not isinstance(cover['id'],str) or not cover['id']
            or not isinstance(cover['absolute_path'],str) or not Path(cover['absolute_path']).is_absolute()
            or not isinstance(cover['sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',cover['sha256'])):
        raise PublishError('封面绑定无效')
    if playlist is not None and (not isinstance(playlist,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',playlist)):
        raise PublishError('合集绑定无效')
    if pos is not None and (playlist is None or type(pos) is not int or pos < 0):
        raise PublishError('合集位置无效')
    if (component['part'] == 'cover' and cover is None) or (component['part'] == 'playlist' and playlist is None):
        raise PublishError('未指定组件')
    return component


def _cover_bytes(cover):
    path = Path(cover['absolute_path'])
    if path.is_symlink(): raise PublishError('封面不能是符号链接')
    try:
        with path.open('rb') as stream: data = stream.read(50*1024*1024+1)
    except OSError:
        raise PublishError('封面不可读') from None
    if not data or len(data)>50*1024*1024 or hashlib.sha256(data).hexdigest()!=cover['sha256']:
        raise PublishError('封面与已授权摘要不符')
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return data,'image/png'
    if data.startswith(b'\xff\xd8\xff'): return data,'image/jpeg'
    raise PublishError('封面必须为 PNG 或 JPEG')


def _component_video(client, video_id, channel):
    q = urllib.parse.urlencode({'part':'snippet','id':video_id})
    code, _, raw = client.call('GET', API+'/youtube/v3/videos?'+q)
    items = json_body(raw).get('items') if code == 200 else None
    if (not isinstance(items,list) or len(items)!=1 or items[0].get('id')!=video_id
            or items[0].get('snippet',{}).get('channelId')!=channel):
        raise PublishError('YouTube 视频和频道身份未核实')
    return items[0]['snippet']


def _playlist_matches(client, playlist_id, video_id, channel, position):
    q=urllib.parse.urlencode({'part':'snippet','id':playlist_id})
    code,_,raw=client.call('GET',API+'/youtube/v3/playlists?'+q)
    items=json_body(raw).get('items') if code==200 else None
    if (not isinstance(items,list) or len(items)!=1 or items[0].get('id')!=playlist_id
            or items[0].get('snippet',{}).get('channelId')!=channel):
        raise PublishError('合集不属于目标频道或不可读')
    params={'part':'snippet','playlistId':playlist_id,'videoId':video_id,'maxResults':50}
    seen=set(); matches=[]
    while True:
        code,_,raw=client.call('GET',API+'/youtube/v3/playlistItems?'+urllib.parse.urlencode(params))
        body=json_body(raw) if code==200 else {}; items=body.get('items')
        if not isinstance(items,list): raise PublishError('合集读回不完整')
        for item in items:
            sn=item.get('snippet',{}) if isinstance(item,dict) else {}
            if (sn.get('playlistId')!=playlist_id or sn.get('resourceId',{}).get('videoId')!=video_id
                    or not isinstance(item.get('id'),str) or not item['id']
                    or (position is not None and sn.get('position')!=position)):
                raise PublishError('合集作品身份或位置不匹配')
            matches.append(item['id'])
        page=body.get('nextPageToken')
        if not page: break
        if not isinstance(page,str) or page in seen or len(seen)>=100: raise PublishError('合集分页无效')
        seen.add(page); params['pageToken']=page
    if len(matches)>1: raise PublishError('合集存在重复作品')
    return matches


def dispatch_component(request_path, action, profile, client_factory=APIClient):
    request=read_json(request_path); manifest=read_json(request['manifest_path'])
    if digest(manifest)!=request.get('manifest_digest'): raise PublishError('原任务快照已变化')
    target=next((x for x in manifest.get('targets',[]) if x.get('id')==request.get('target',{}).get('id')),None)
    if not target or target!=request['target']: raise PublishError('原目标已变化')
    channel,token=config(profile,target,writing=action=='component')
    part=_component_binding(request,channel); client=client_factory(token); identity(client,channel)
    video_id=part['video_id']; snippet=_component_video(client,video_id,channel)
    result={k:part[k] for k in ('component_key','original_job_key','video_id','part')}
    if part['part']=='playlist':
        playlist=part['playlist_id']; position=part['playlist_position']
        matches=_playlist_matches(client,playlist,video_id,channel,position)
        if action=='verify_component':
            result['state']='verified' if len(matches)==1 else 'unknown'
            return result
        if matches:
            result['state']='accepted'; return result
        try: scopes=client.granted_scopes()
        except PublishError: scopes=set()
        if not scopes.intersection({'https://www.googleapis.com/auth/youtube','https://www.googleapis.com/auth/youtube.force-ssl','https://www.googleapis.com/auth/youtubepartner'}):
            result.update(state='needs_authorization',no_write=True,reason='scope_unconfirmed'); return result
        sn={'playlistId':playlist,'resourceId':{'kind':'youtube#video','videoId':video_id}}
        if position is not None: sn['position']=position
        code,_,raw=client.call('POST',API+'/youtube/v3/playlistItems?part=snippet',
            headers={'Content-Type':'application/json; charset=UTF-8'},data=json.dumps({'snippet':sn}).encode())
        body=json_body(raw) if code in (200,201) else {}; got=body.get('snippet',{})
        if (not isinstance(body.get('id'),str) or not body['id'] or got.get('playlistId')!=playlist
                or got.get('resourceId',{}).get('videoId')!=video_id):
            raise PublishError('合集请求结果未知')
        result['state']='accepted'; return result
    cover=part['cover']
    if action=='component':
        data,mime=_cover_bytes(cover)
        q=urllib.parse.urlencode({'videoId':video_id,'uploadType':'media'})
        code,_,raw=client.call('POST',API+'/upload/youtube/v3/thumbnails/set?'+q,
            headers={'Content-Type':mime,'Content-Length':str(len(data))},data=data)
        body=json_body(raw) if code in (200,201) else {}
        if not isinstance(body.get('items'),list) or len(body['items'])!=1:
            raise PublishError('封面请求结果未知')
        result['state']='accepted'; return result
    thumbnails=snippet.get('thumbnails',{})
    url=next((thumbnails[k].get('url') for k in ('maxres','standard','high','medium','default')
              if isinstance(thumbnails.get(k),dict) and thumbnails[k].get('url')),None) if isinstance(thumbnails,dict) else None
    if not url: result['state']='unknown'; return result
    image=client.thumbnail(url,video_id)
    if not isinstance(image,bytes) or not image or len(image)>16*1024*1024:
        result['state']='unknown'; return result
    if not (image.startswith(b'\xff\xd8\xff') or (image.startswith(b'RIFF') and image[8:12]==b'WEBP')):
        result['state']='unknown'; return result
    sha=hashlib.sha256(image).hexdigest()
    path=Path(str(request_path)+'.thumbnail-'+sha)
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as out: out.write(image); out.flush(); os.fsync(out.fileno())
    except FileExistsError:
        if (path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.getuid()
                or path.stat().st_mode & 0o077 or file_hash(path)!=sha):
            raise PublishError('封面观察文件不可信')
    result.update(state='needs_visual_review',thumbnail_url=url,thumbnail_path=str(path.resolve()),
                  thumbnail_sha256=sha,cover_sha256=cover['sha256'])
    return result


def dispatch(request_path, action, profile, client_factory=APIClient):
    if action in {'component','verify_component'}:
        return dispatch_component(request_path,action,profile,client_factory)
    request = read_json(request_path); manifest = read_json(request['manifest_path'])
    if digest(manifest) != request['manifest_digest']: raise PublishError('YouTube 封存快照变化')
    target = next((x for x in manifest.get('targets', []) if x['id'] == request['target']['id']), None)
    if target != request['target']: raise PublishError('YouTube 目标与封存快照不符')
    channel, token = config(profile, target, writing=action != 'verify')
    pack = build_pack(manifest, target['id'], profile, check_files=action != 'verify')
    bundle=request.get('youtube_bundle',False)
    if type(bundle) is not bool: raise PublishError('YouTube 组合任务标记无效')
    video, resource = make_plan(pack,bundle=bundle)
    binding = {'manifest_digest':digest(manifest), 'target_id':target['id'],
               'account':target['account'], 'channel':channel, 'asset_sha256':video['sha256'],
               'resource_digest':digest(resource)}
    cp_path = checkpoint_path(request_path)
    client = client_factory(token); identity(client, channel)
    if action == 'execute':
        if cp_path.exists(): raise PublishError('已有 YouTube 执行记录；禁止重复建会话')
        path, size = create_snapshot(video, request_path)
        write_json(cp_path, {'binding':binding,'phase':'initiating','size':size})
        payload = json.dumps(resource, ensure_ascii=False).encode('utf-8')
        code, headers, _ = client.call('POST', API+'/upload/youtube/v3/videos?uploadType=resumable&part=snippet%2Cstatus',
            headers={'Content-Type':'application/json; charset=UTF-8','X-Upload-Content-Length':str(size),
                     'X-Upload-Content-Type':'application/octet-stream'}, data=payload)
        if code != 200: raise PublishError('YouTube 创建会话结果未知')
        cp = {'binding':binding,'phase':'session','size':size,'session_url':session_url(header(headers,'Location'))}
        write_json(cp_path, cp)
        video_id = transfer(client, cp, path, size, 0, cp_path)
        if file_hash(path) != video['sha256']: raise PublishError('上传期间视频已变化，保持结果未知')
    else:
        cp = read_checkpoint(cp_path, binding); size = cp.get('size')
        if type(size) is not int or size <= 0: raise PublishError('上传会话大小无效')
        if cp['phase'] == 'completed' and isinstance(cp.get('video_id'), str) and VIDEO_ID.fullmatch(cp['video_id']):
            video_id, offset = cp['video_id'], size
        else:
            video_id, offset = probe(client, cp, size)
        if video_id:
            cp.update(phase='completed', video_id=video_id); write_json(cp_path, cp)
        elif action == 'continue':
            path = check_snapshot(video, request_path, size)
            video_id = transfer(client, cp, path, size, offset, cp_path)
            if file_hash(path) != video['sha256']: raise PublishError('上传期间视频已变化，保持结果未知')
        else:
            raise PublishError('原上传仍未完成；需在原授权下显式继续原会话')
    if request['job'].get('remote_id') and request['job']['remote_id'] != video_id:
        raise PublishError('YouTube 原作品 ID 发生变化')
    observation = {'manifest_digest':digest(manifest),'target_id':target['id'],'platform':'youtube',
                   'account':target['account'],'state':'pending','remote_id':video_id,
                   'remote_url':'https://www.youtube.com/watch?v='+video_id,
                   'observed_account_identity':channel}
    if action != 'verify': return observation
    check_snapshot(video, request_path, size)
    q = urllib.parse.urlencode({'part':'snippet,status','id':video_id})
    code, _, raw = client.call('GET', API+'/youtube/v3/videos?'+q)
    if code != 200: raise PublishError('YouTube 作品读回失败')
    items = json_body(raw).get('items')
    if not isinstance(items, list) or len(items) != 1: raise PublishError('YouTube 作品读回不唯一')
    item = items[0]; sn = item.get('snippet',{}); st = item.get('status',{})
    if (item.get('id') != video_id or sn.get('channelId') != channel
            or sn.get('title') != resource['snippet']['title']
            or sn.get('description','') != resource['snippet']['description']
            or not tags_equal(sn.get('tags',[]), resource['snippet'].get('tags',[]))
            or st.get('privacyStatus') != 'public'
            or st.get('selfDeclaredMadeForKids') != resource['status']['selfDeclaredMadeForKids']):
        raise PublishError('YouTube 独立读回不匹配；不标记成功')
    if ('containsSyntheticMedia' in resource['status'] and
            st.get('containsSyntheticMedia') != resource['status']['containsSyntheticMedia']):
        raise PublishError('YouTube 合成媒体声明未读回')
    if ('categoryId' in resource['snippet'] and sn.get('categoryId') != resource['snippet']['categoryId']):
        raise PublishError('YouTube 分类未读回')
    if st.get('uploadStatus') != 'processed': return observation
    public = client.public_video(video_id)
    thumbnail = urllib.parse.urlsplit(public.get('thumbnail_url', '') if isinstance(public, dict) else '')
    path_parts = thumbnail.path.strip('/').split('/')
    if (not isinstance(public, dict) or public.get('title') != resource['snippet']['title']
            or thumbnail.scheme != 'https' or thumbnail.hostname != 'i.ytimg.com'
            or len(path_parts) < 3 or path_parts[0] not in {'vi','vi_webp'} or path_parts[1] != video_id):
        return observation
    if bundle:
        observation.update(state='pending',video_only_verified=True,stage='youtube_components_pending',
                           verified_pack_digest=digest(pack),verified_asset_ids=[video['id']])
    else:
        observation.update(state='published',content_verified=True,verified_pack_digest=digest(pack),
                           verified_asset_ids=[video['id']])
    return observation


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('execute','verify','continue','component','verify_component'))
    parser.add_argument('--request',required=True); parser.add_argument('--profile',required=True)
    args=parser.parse_args()
    try:
        fd=int(os.environ['SANDY_PUBLISH_LOCK_FD']); os.fstat(fd)
        print(json.dumps(dispatch(args.request,args.action,load_profile(args.profile)),ensure_ascii=False))
        return 0
    except Exception:
        # A raw exception may contain an OAuth token or session URI; never expose it.
        print(json.dumps({'ok':False,'error':'YouTube 适配器未完成；保持原任务并先核验'},ensure_ascii=False),file=sys.stderr)
        return 2

if __name__=='__main__': sys.exit(main())
