import unittest
from unittest.mock import MagicMock
import browser_runtime as br
from publish_core import PublishError
def conn(n):
    ctx=MagicMock();pages=[]
    for i in range(n):
        p=MagicMock();p.url='https://x.com/home';p.context=ctx;pages.append(p)
    ctx.pages=pages;c=MagicMock();c.contexts=[ctx];return c,pages
class T(unittest.TestCase):
    def test_duplicate_home_anchor_ok(self):
        c,p=conn(2);self.assertIs(br.select_page(c,'x',path='/home',anchor=True),p[0])
    def test_duplicate_without_anchor_still_rejected(self):
        c,_=conn(2)
        with self.assertRaises(PublishError):br.select_page(c,'x',path='/home')
    def test_anchor_requires_path(self):
        c,_=conn(2)
        with self.assertRaises(PublishError):br.select_page(c,'x',anchor=True)
