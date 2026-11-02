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
        except Exception: pass  # enforced by assert_visible below
        page.goto('https://x.com/compose/post', wait_until='domcontentloaded', timeout=25000)
        node = wait_composer(page)
        assert_visible(page)
        return node
    except Exception as exc:
        self_check(page, saved, marker, identity)
        if isinstance(exc, PublishError):
            raise
        raise PublishError('X 后台编辑页未就绪；已只读收尾，不选文件、不发送') from exc


def wait_painted(page, predicate, *, timeout_ms=120000, slice_ms=5000, polling=250):
    """Wait for a page predicate while re-realizing the unfocused window each slice.

    Chrome may stop painting / decoding media in an unfocused, unpainted window, so
    X never starts its upload after file selection. A native capture per slice keeps
    the surface realized without activating it. Bounded; raises PublishError."""
    waited = 0
    while waited < timeout_ms:
        try:
            realize(page)
        except Exception:
            pass
        try:
            page.wait_for_function(predicate, timeout=slice_ms, polling=polling)
            return
        except Exception:
            waited += slice_ms
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
