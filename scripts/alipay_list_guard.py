"""支付宝生活号内容管理列表：发前查重/发后读回的判定核心，以及「业务修订重发」任务记录。

只做纯判定与本地私有记录，不连接浏览器、不点击发布。浏览器侧只负责把观察结果
（页面 URL、是否看到平台显示的生活号 ID、各页 publishList 原始回应）交给 evaluate()。

判定原则（fail closed）：
- 页面不在 c.alipay.com 内容管理页、未看到绑定生活号 ID、没有任何列表回应、回应非 ok、
  缺 total、或收集到的作品数少于 total（分页未读完）→ unverifiable，不得当作「无同名作品」。
- 同名命中全部属于本次重发任务声明替代的旧作品 ID → superseded_only（仅重发任务可放行）。
- 其他同名命中 → duplicate。
"""
import argparse,datetime,hashlib,json,os,sys
from pathlib import Path
from urllib.parse import urlparse

HOST='c.alipay.com'; LIST_PATH='/page/content-creation/posts'
CLEAR,SUPERSEDED,DUPLICATE,UNVERIFIABLE='clear','superseded_only','duplicate','unverifiable'
EXIT={CLEAR:0,SUPERSEDED:0,DUPLICATE:2,UNVERIFIABLE:5}
FIELDS=('contentId','title','state','sendTime','duration','imageUrl','permissionLevel','transcodeFailed',
        'auditReason','qualityAuditReason','secAuditReason','showQualityAuditAppealEntrance')

def title_key(ep,match=None):
    """同名判定子串。默认《初馨》「「初馨」S6E01｜」；其他系列（如《上下五千年 | 第 1 集：盘古开天》）
    传 match 显式给出能唯一识别该集的标题子串，不再套用初馨季集号格式。"""
    if match is not None:
        if not str(match).strip():raise ValueError('match 不能为空')
        return str(match)
    return f'「初馨」{ep}｜'

def _items(responses):
    seen={};totals=[];errors=[]
    for r in responses:
        if not isinstance(r,dict) or r.get('stat')!='ok' or not isinstance(r.get('result'),dict):
            errors.append('列表回应非 ok');continue
        res=r['result'];t=res.get('total')
        if isinstance(t,int) and t>=0:totals.append(t)
        else:errors.append('列表回应缺 total')
        rows=res.get('publishContents')
        if not isinstance(rows,list):errors.append('列表回应缺 publishContents');continue
        for i in rows:
            if isinstance(i,dict) and i.get('contentId') and isinstance(i.get('title'),str):seen[i['contentId']]=i
    return seen,totals,errors

def evaluate(page_url,account_id_seen,responses,ep,supersedes=(),expected_new=0,match=None):
    """返回 dict(status, reasons, hits, new_hits, total, collected)。expected_new>0 用于发后读回。"""
    reasons=[];u=urlparse(page_url or '')
    if u.scheme!='https' or u.hostname!=HOST or u.path!=LIST_PATH:reasons.append(f'页面不在支付宝内容管理页: {u.hostname}{u.path}')
    if account_id_seen is not True:reasons.append('未观察到平台显示的生活号 ID（可能被跳登录）')
    if not responses:reasons.append('未拿到 publishList 列表回应')
    seen,totals,errs=_items(responses or []);reasons+=errs
    total=max(totals) if totals else None
    if total is not None and len(seen)<total:reasons.append(f'分页未读完：收集 {len(seen)} / total {total}')
    if total==0 and not reasons:pass
    hits=[{k:i.get(k) for k in FIELDS} for i in seen.values() if title_key(ep,match) in i['title']]
    sup=set(supersedes or ());new=[h for h in hits if h['contentId'] not in sup]
    out=dict(ep=ep,total=total,collected=len(seen),hits=hits,new_hits=new,supersedes=sorted(sup),
             missing_superseded=sorted(sup-{h['contentId'] for h in hits}))
    if reasons:out.update(status=UNVERIFIABLE,reasons=reasons);return out
    if sup and out['missing_superseded']:
        out.update(status=UNVERIFIABLE,reasons=['声明替代的旧作品 ID 未在完整列表中出现，不能确认替代对象']);return out
    if expected_new:
        out.update(status=CLEAR if len(new)==expected_new else DUPLICATE,reasons=[] if len(new)==expected_new else [f'新作品命中 {len(new)} 条，期望 {expected_new}']);return out
    if not hits:out.update(status=CLEAR,reasons=[])
    elif not new:out.update(status=SUPERSEDED,reasons=[])
    else:out.update(status=DUPLICATE,reasons=['已有同名作品且不在本次替代清单'])
    return out

def evaluate_series(page_url,account_id_seen,responses,eps,matches=None):
    """季收尾核对（发后，不是发前查重）：每集同名作品恰好 1 条。
    缺集或某集多于 1 条 → duplicate（不通过）；列表不可完整读取 → unverifiable。
    教训（2026-11-04 S5）：收尾用单集发前查重会把刚发的那集判成「已有同名作品」。"""
    base=evaluate(page_url,account_id_seen,responses,eps[0],match=(matches or {}).get(eps[0]))
    out=dict(eps=list(eps),total=base['total'],collected=base['collected'],per_ep={},reasons=[])
    if base['status']==UNVERIFIABLE:
        out.update(status=UNVERIFIABLE,reasons=base['reasons']);return out
    seen,_,_=_items(responses)
    for ep in eps:
        hits=[{k:i.get(k) for k in FIELDS} for i in seen.values() if title_key(ep,(matches or {}).get(ep)) in i['title']]
        out['per_ep'][ep]=hits
        if len(hits)!=1:out['reasons'].append(f'{ep} 同名作品 {len(hits)} 条，期望 1')
    out['status']=CLEAR if not out['reasons'] else DUPLICATE
    return out

def sha256(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()

def create_resend(ledger,ep,video,cover,title,description,supersedes,revision,now=None):
    """业务修订重发：每集新建一条私有任务记录（追加写），登记新视频 sha256 与替代的旧作品 ID。
    同一集同一视频 sha256 已有记录→拒绝（不能靠换任务名重发同一任务）；revision 必填。"""
    if not (revision or '').strip():raise ValueError('重发必须写明业务修订')
    if not supersedes:raise ValueError('重发必须声明替代的旧作品 ID')
    for p in (video,cover):
        if not Path(p).is_file():raise ValueError(f'文件不存在: {p}')
    v=sha256(video);lp=Path(ledger);rows=[]
    if lp.exists():rows=[json.loads(l) for l in lp.read_text().splitlines() if l.strip()]
    for r in rows:
        if r['ep']==ep and r['video_sha256']==v:raise ValueError(f'{ep} 同一视频已有重发任务 {r["task_id"]}，不重复建')
        if set(r['supersedes'])&set(supersedes) and r['ep']==ep:raise ValueError(f'{ep} 旧作品已被任务 {r["task_id"]} 声明替代')
    ts=(now or datetime.datetime.now().astimezone()).isoformat(timespec='seconds')
    rec=dict(task_id=f'alipay-resend-{ep}-{v[:12]}',platform='alipay',ep=ep,title=title,description=description,
             video=str(video),video_sha256=v,cover=str(cover),cover_sha256=sha256(cover),supersedes=list(supersedes),
             revision=revision.strip(),created_at=ts)
    lp.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(lp,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a') as f:f.write(json.dumps(rec,ensure_ascii=False)+'\n')
    return rec

def load_task(ledger,ep):
    rows=[json.loads(l) for l in Path(ledger).read_text().splitlines() if l.strip()]
    m=[r for r in rows if r['ep']==ep]
    if len(m)!=1:raise ValueError(f'{ep} 重发任务应恰好 1 条，实际 {len(m)}')
    return m[0]

def main(argv=None):
    a=argparse.ArgumentParser(description=__doc__.split('\n')[0]);s=a.add_subparsers(dest='cmd',required=True)
    c=s.add_parser('create-resend');[c.add_argument(x,required=True) for x in ('--ledger','--ep','--video','--cover','--title','--description','--revision')]
    c.add_argument('--supersedes',action='append',required=True)
    e=s.add_parser('evaluate');e.add_argument('observation');e.add_argument('--ep',required=True);e.add_argument('--supersedes',action='append',default=[]);e.add_argument('--expected-new',type=int,default=0);e.add_argument('--match',default=None,help='非初馨系列：能唯一识别该集的标题子串')
    n=a.parse_args(argv)
    if n.cmd=='create-resend':
        try:print(json.dumps(create_resend(n.ledger,n.ep,n.video,n.cover,n.title,n.description,n.supersedes,n.revision),ensure_ascii=False));return 0
        except ValueError as x:print(json.dumps({'error':str(x)},ensure_ascii=False));return 2
    o=json.loads(Path(n.observation).read_text())
    r=evaluate(o.get('url'),o.get('account_id_seen'),o.get('responses'),n.ep,n.supersedes,n.expected_new,n.match)
    print(json.dumps(r,ensure_ascii=False,indent=1));return EXIT[r['status']]
if __name__=='__main__':sys.exit(main())
