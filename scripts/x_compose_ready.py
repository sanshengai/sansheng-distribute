"""X only: realize a background composer window and stop read-only when it never renders.

Root cause (2026-10-31 S3E02): a new unfocused X window can stay unpainted, so the
modal textarea exists but is not "visible" to Playwright and the 20 s wait timed
out. A native surface capture (pixels discarded) realizes it without activating
Chrome or the tab. Retries are bounded; on failure the same window is checked
read-only and the result is stored in the attempt record, so `resume` can prove
"nothing selected or sent" instead of leaving an unrecoverable unknown.
"""
import time
from publish_core import PublishError, write_json

TEXTAREA = '[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_0"]'
ROUNDS = 6
ROUND_MS = 8000


def realize(page):
    session = page.context.new_cdp_session(page)
    try:
        session.send('Page.captureScreenshot', dict(format='png', fromSurface=True, captureBeyondViewport=False))
    finally:
        session.detach()


def wait_composer(page, *, rounds=ROUNDS, round_ms=ROUND_MS):
    node = page.locator(TEXTAREA)
    for _ in range(rounds):
        try:
            realize(page)
        except Exception:
            pass
        try:
            node.wait_for(state='visible', timeout=round_ms)
            return node
        except Exception:
            continue
    raise PublishError('X 后台编辑框在上限内未渲染；已只读收尾，不选文件、不发送')


def self_check(page, saved, marker, identity):
    """Read-only proof on the same owned window: right page/account, empty editor, no media."""
    import browser_engine as engine
    value = dict(at=time.time(), ok=False)
    try:
        try:
            realize(page)
        except Exception:
            pass
        engine.assert_identity(page, identity); engine.assert_no_warning(page)
        engine.empty_editor(page)
        if page.locator('input[type=file]').evaluate_all('(ns)=>ns.some(n=>n.files&&n.files.length)'):
            raise PublishError('已选文件')
        value.update(ok=True, url=str(page.url))
    except Exception as exc:
        value['error'] = type(exc).__name__
    saved['preinput_self_check'] = value
    write_json(marker, saved)
    return value['ok']


def open_composer(page, saved, marker, identity):
    """Navigate the owned window and wait for the composer; on failure self-check then stop."""
    try:
        try: emulate_focus(page)
        except Exception: pass  # re-established and verified by ensure_focus below
        page.goto('https://x.com/compose/post', wait_until='domcontentloaded', timeout=25000)
        node = wait_composer(page)
        def record(log):
            saved['focus_attempts'] = log; write_json(marker, saved)
        ensure_focus(page, record=record)
        return node
    except Exception as exc:
        self_check(page, saved, marker, identity)
        if isinstance(exc, PublishError):
            raise
        raise PublishError('X 后台编辑页未就绪；已只读收尾，不选文件、不发送') from exc


def wait_painted(page, predicate, *, timeout_ms=120000, slice_ms=5000, polling=250,
                 progress=None, idle_ms=180000, cap_ms=1200000, record=None):
    """Wait for a page predicate while re-realizing the unfocused window each slice.

    Chrome may stop painting / decoding media in an unfocused, unpainted window, so
    X never starts its upload after file selection. A native capture per slice keeps
    the surface realized without activating it. Bounded; raises PublishError.

    progress: optional callable returning a monotonically growing marker (e.g. the
    number of native upload receipts). 2026-11-02 S4E02: a 72 MB upload was still
    receiving chunk receipts (INIT, then APPENDMULTI at 66/67/68/104 s) when the old
    fixed 120 s limit fired. With progress, the wait only times out after idle_ms
    without any new progress, and never beyond cap_ms in total."""
    waited = idle = 0
    last = progress() if progress else None
    limit_total = cap_ms if progress else timeout_ms
    while waited < limit_total and (progress is None or idle < idle_ms):
        try:
            realize(page)
        except Exception:
            pass
        try:
            page.wait_for_function(predicate, timeout=slice_ms, polling=polling)
            return
        except Exception:
            waited += slice_ms; idle += slice_ms
        if progress:
            now = progress()
            if now != last:
                last, idle = now, 0
            if record is not None:
                record(dict(waited_ms=waited, idle_ms=idle, progress=now))
    if progress and idle >= idle_ms:
        raise PublishError(f'X 后台视频上传 {idle_ms//1000}s 无新进展；保留原窗口只读核对，不重传')
    raise PublishError('X 后台视频处理在上限内未就绪；保留原窗口只读核对，不重传')


DECODE_CHECK = True  # tests with synthetic fixtures switch this off in conftest


def decodable(path, *, runner=None, force=False):
    """Local ffprobe decode check of the first video frames before any platform upload."""
    if not (DECODE_CHECK or force):
        return None
    import shutil, subprocess
    runner = runner or subprocess.run
    exe = shutil.which('ffprobe')
    if not exe:
        raise PublishError('缺少 ffprobe，无法在上传前确认视频可解码')
    p = runner([exe, '-v', 'error', '-select_streams', 'v:0', '-read_intervals', '%+2', '-count_frames',
                '-show_entries', 'stream=codec_name,nb_read_frames', '-of', 'json', str(path)],
               capture_output=True, text=True, timeout=120)
    try:
        import json
        stream = json.loads(p.stdout)['streams'][0]
        frames = int(stream.get('nb_read_frames') or 0)
    except Exception:
        frames = 0
    if p.returncode != 0 or frames <= 0 or (p.stderr or '').strip():
        raise PublishError('本地 ffprobe 未能解码视频开头；不上传')
    return frames


class KeepAwake:
    """One-shot `caffeinate -d -i -w <pid>` tied to this process; no system setting changes."""
    def __init__(self, *, popen=None, which=None):
        import shutil, subprocess
        self.popen = popen or subprocess.Popen; self.which = which or shutil.which; self.proc = None
    def __enter__(self):
        import os
        exe = self.which('caffeinate')
        if exe:
            try:
                self.proc = self.popen([exe, '-d', '-i', '-w', str(os.getpid())])
            except OSError:
                self.proc = None
        return self
    def __exit__(self, *exc):
        if self.proc is not None:
            try: self.proc.terminate()
            except Exception: pass
        return False


_HELD = {}


def emulate_focus(page):
    """Hold CDP focus emulation for the owned window's whole lifetime (no OS activation).

    Emulation lives on the CDP session: detaching it (as the first version did)
    silently returns the unfocused window to visibilityState=hidden, where X's
    composer accepts a file but never starts its upload (2026-11-01 evidence).
    The session is kept until the page closes or the process exits."""
    key = id(page)
    if key in _HELD:
        return _HELD[key]
    session = page.context.new_cdp_session(page)
    session.send('Emulation.setFocusEmulationEnabled', {'enabled': True})
    try: session.send('Page.setWebLifecycleState', {'state': 'active'})
    except Exception: pass
    _HELD[key] = session
    return session


VISIBLE_CHECK = True


def release_focus(page):
    """Drop a held emulation session (it may belong to a dead renderer)."""
    session = _HELD.pop(id(page), None)
    if session is not None:
        try: session.detach()
        except Exception: pass


def ensure_focus(page, *, attempts=4, wait_ms=750, record=None, force=False):
    """Hold focus emulation and prove it took effect, re-establishing it if not.

    2026-11-02 S4E02: a freshly opened owned window stayed hidden/unfocused at the
    composer check. Emulation was requested once on about:blank before the
    cross-origin navigation and any failure was swallowed, so a session that never
    applied (or was lost with the renderer swap) could not recover. Now: (re)hold
    after navigation, verify, and on failure detach + re-create the session and wait,
    bounded; every attempt is recorded. Never activates the window."""
    if not (VISIBLE_CHECK or force):
        return None
    log = []
    for n in range(attempts):
        try:
            emulate_focus(page); err = None
        except Exception as exc:
            err = type(exc).__name__
        try:
            state = page.evaluate('({vis:document.visibilityState,focus:document.hasFocus()})')
        except Exception as exc:
            state = {}; err = err or type(exc).__name__
        ok = isinstance(state, dict) and state.get('vis') == 'visible' and state.get('focus') is True
        log.append(dict(attempt=n + 1, vis=state.get('vis') if isinstance(state, dict) else None,
                        focus=state.get('focus') if isinstance(state, dict) else None, error=err))
        if record is not None:
            record(log)
        if ok:
            return state
        release_focus(page)
        page.wait_for_timeout(wait_ms)
    raise PublishError(f'X 后台窗口重试 {attempts} 次仍未进入可见/聚焦模拟；不选文件（最后状态 {log[-1]}）')


def assert_visible(page, force=False):
    if not (VISIBLE_CHECK or force):
        return None
    state = page.evaluate('({vis:document.visibilityState,focus:document.hasFocus()})')
    if state.get('vis') != 'visible' or state.get('focus') is not True:
        raise PublishError('X 后台窗口未进入可见/聚焦模拟；不选文件')
    return state


class Diagnostics:
    """Passive console/network notes for a stalled upload. No bodies, queries, cookies or headers."""
    LIMIT = 60
    def __init__(self, page):
        self.page = page; self.rows = []
    def _add(self, row):
        if len(self.rows) < self.LIMIT: self.rows.append(dict(row, t=round(time.time(), 3)))
    def _console(self, msg):
        try:
            if msg.type in ('error', 'warning'):
                self._add(dict(kind='console', level=msg.type, text=str(msg.text)[:200]))
        except Exception:
            pass
    def _response(self, res):
        try:
            from urllib.parse import urlsplit
            u = urlsplit(res.url)
            if res.status >= 400 or 'upload' in u.netloc or '/media' in u.path:
                self._add(dict(kind='response', host=u.netloc, path=u.path[:120], status=res.status))
        except Exception:
            pass
    def _failed(self, req):
        try:
            from urllib.parse import urlsplit
            u = urlsplit(req.url)
            self._add(dict(kind='requestfailed', host=u.netloc, path=u.path[:120], error=str(req.failure)[:120]))
        except Exception:
            pass
    def __enter__(self):
        self.page.on('console', self._console); self.page.on('response', self._response)
        self.page.on('requestfailed', self._failed); return self
    def __exit__(self, *exc):
        for n, f in (('console', self._console), ('response', self._response), ('requestfailed', self._failed)):
            try: self.page.remove_listener(n, f)
            except Exception: pass
        return False
    def snapshot(self):
        try:
            state = self.page.evaluate("""()=>{const m=document.querySelector('[role=dialog][aria-modal=true]');
              return {vis:document.visibilityState,focus:document.hasFocus(),
                inputs:m?[...m.querySelectorAll('input[type=file]')].map(i=>i.files.length):[],
                attachments:m?m.querySelectorAll('[data-testid=attachments]').length:0,
                videos:m?m.querySelectorAll('video').length:0,
                alerts:[...document.querySelectorAll('[role=alert]')].map(a=>a.innerText.slice(0,120))}}""")
        except Exception as exc:
            state = dict(error=type(exc).__name__)
        return dict(events=self.rows, page=state)
