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


def ensure(page):
    """Hold emulation and assert it took effect; raise before acting otherwise."""
    if hold(page) is None:
        return None
    state = page.evaluate('({vis:document.visibilityState,focus:document.hasFocus()})')
    if not isinstance(state, dict) or state.get('vis') != 'visible' or state.get('focus') is not True:
        raise PublishError('抖音后台窗口未进入可见/聚焦模拟；停在动作前')
    return state
