"""Explicit, lossless-to-text projection for accounts without external links."""
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from publish_core import PublishError, visible_text


def project_links(content):
    """Return projected HTML, stripped-link baseline, and an auditable change list.

    Preserve the source bytes except for external anchor tags. Never treat a
    missing URL as equivalent to a complete projection.
    """
    class Anchors(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.offsets = [0]
            for line in content.splitlines(keepends=True):
                self.offsets.append(self.offsets[-1] + len(line))
            self.active = None
            self.rows = []

        def source_offset(self):
            line, column = self.getpos()
            return self.offsets[line-1] + column

        def handle_starttag(self, tag, attrs):
            if tag != 'a':
                return
            if self.active:
                raise PublishError('嵌套链接无法安全转换')
            href = dict(attrs).get('href', '')
            url = urlsplit(href)
            if url.scheme in {'https', 'http'} and url.hostname != 'mp.weixin.qq.com':
                start = self.source_offset()
                self.active = (start, start + len(self.get_starttag_text()), href)

        def handle_endtag(self, tag):
            if tag == 'a' and self.active:
                start, inner, href = self.active
                end = self.source_offset()
                close = content.find('>', end) + 1
                self.rows.append((start, inner, end, close, href))
                self.active = None

    parser = Anchors()
    parser.feed(content)
    parser.close()
    if parser.active:
        raise PublishError('未闭合链接无法安全转换')
    projected, stripped, changes = content, content, []
    for start, inner, end, close, href in reversed(parser.rows):
        label = content[inner:end]
        suffix = '' if visible_text(label) == href else '（' + escape(href) + '）'
        projected = projected[:start] + label + suffix + projected[close:]
        stripped = stripped[:start] + label + stripped[close:]
        changes.insert(0, {'label': visible_text(label), 'url': href,
                           'replacement_text': visible_text(label + suffix)})
    return projected, stripped, changes
