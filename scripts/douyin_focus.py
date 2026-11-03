"""Douyin: keep an owned background window behaving as visible and focused, without
bringing Chrome to the foreground.

Chrome reports an unfocused background window as visibilityState=hidden and
hasFocus()=false; Douyin's editor then drops its selection, so the native
「#添加话题」 button inserts nothing (2026-11-01 evidence, same root cause the X
executor found). CDP focus emulation lives on its session, so the session is held
(never detached) for the page's lifetime. Before any editor action we assert
visible+focused and stop before the action otherwise.
"""
from publish_core import PublishError

_HELD = {}
# Unit tests of other modules use fake pages without CDP; they switch this off.
ENABLED = True


def hold(page):
    if not ENABLED:
        return None
    context = getattr(page, 'context', None)
    if context is None or not hasattr(context, 'new_cdp_session'):
        return None  # unit-test fakes only; real Playwright pages always have a context
    key = id(page)
    if key in _HELD:
        return _HELD[key]
    session = context.new_cdp_session(page)
    session.send('Emulation.setFocusEmulationEnabled', {'enabled': True})
    try: session.send('Page.setWebLifecycleState', {'state': 'active'})
    except Exception: pass
    _HELD[key] = session
    return session


def release(page):
    """Drop a held emulation session (it may belong to a swapped/dead renderer)."""
    session = _HELD.pop(id(page), None)
    if session is not None:
        try: session.detach()
        except Exception: pass


def _state(page):
    try:
        return page.evaluate('({vis:document.visibilityState,focus:document.hasFocus()})'), None
    except Exception as exc:
        return {}, type(exc).__name__


def ensure(page, *, attempts=4, wait_ms=750, record=None):
    """Hold emulation and prove it took effect, re-establishing it if not; raise before acting.

    2026-11-03 S5E02: the session was cached by id(page) from an earlier hold (before the
    cross-origin navigation / renderer swap), so a single check found the window
    hidden/unfocused and stopped with no way to recover. Same root cause and fix as
    X ensure_focus: verify, on failure detach + re-create the session and wait, bounded;
    every attempt is recorded. Never activates the OS window."""
    if not ENABLED:
        return None
    context = getattr(page, 'context', None)
    if context is None or not hasattr(context, 'new_cdp_session'):
        return None  # unit-test fakes only
    log = []
    for n in range(attempts):
        err = None
        try:
            hold(page)
        except Exception as exc:
            err = type(exc).__name__
        state, e2 = _state(page); err = err or e2
        ok = isinstance(state, dict) and state.get('vis') == 'visible' and state.get('focus') is True
        log.append(dict(attempt=n + 1, vis=state.get('vis') if isinstance(state, dict) else None,
                        focus=state.get('focus') if isinstance(state, dict) else None, error=err))
        if record is not None:
            record(log)
        if ok:
            return state
        release(page)
        if n + 1 < attempts:
            try: page.wait_for_timeout(wait_ms)
            except Exception: pass
    raise PublishError(f'抖音后台窗口重试 {attempts} 次仍未进入可见/聚焦模拟；停在动作前（最后状态 {log[-1]}）')
