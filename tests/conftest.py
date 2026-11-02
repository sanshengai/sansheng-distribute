import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import x_compose_ready
# Synthetic MP4 fixtures are not real video; the real decode check is covered with force=True.
x_compose_ready.DECODE_CHECK = False

try:
    import x_compose_ready as _xr
    _xr.VISIBLE_CHECK = False
except Exception:
    pass


import pytest
@pytest.fixture(autouse=True)
def _douyin_focus_off_for_fakes(request, monkeypatch):
    """Fake Douyin pages have no real CDP; focus emulation is covered by test_douyin_focus."""
    if request.module.__name__.startswith(('test_douyin','test_weibo')) and request.module.__name__ != 'test_douyin_focus':
        import douyin_focus
        monkeypatch.setattr(douyin_focus, 'ENABLED', False)
