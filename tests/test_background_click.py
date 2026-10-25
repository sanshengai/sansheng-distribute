import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from browser_runtime import click_background
from publish_core import PublishError


class Node:
    def __init__(self,states,counts=(1,1)):
        self.states=iter(states);self.counts=iter(counts);self.reads=0
    def count(self):return next(self.counts)
    def evaluate(self,script):self.reads+=1;return next(self.states)


class Page:
    def __init__(self):self.mouse=self;self.clicks=[]
    def click(self,x,y):self.clicks.append((x,y))
    def bring_to_front(self):raise AssertionError('must never activate Chrome')


class BackgroundClickTests(unittest.TestCase):
    def setUp(self):
        self.page=Page();self.good=dict(x=100,y=80,width=30,height=20,ready=True)
        sleep=patch('browser_runtime.time.sleep');sleep.start();self.addCleanup(sleep.stop)
    def test_one_native_click_after_two_stable_reads(self):
        node=Node([self.good,self.good]);click_background(self.page,node)
        self.assertEqual(node.reads,2);self.assertEqual(self.page.clicks,[(100,80)])
    def test_hidden_obscured_disabled_or_outside_stops_without_click(self):
        with self.assertRaises(PublishError):click_background(self.page,Node([dict(self.good,ready=False)]))
        self.assertEqual(self.page.clicks,[])
    def test_moving_or_changed_target_stops_without_click(self):
        for changed in (dict(self.good,x=101),dict(self.good,width=31),dict(self.good,ready=False)):
            with self.assertRaises(PublishError):click_background(self.page,Node([self.good,changed]))
        self.assertEqual(self.page.clicks,[])
    def test_missing_duplicate_or_detached_target_is_not_clicked(self):
        for counts in ((0,),(2,),(1,0),(1,2)):
            with self.assertRaises(PublishError):click_background(self.page,Node([self.good,self.good],counts))
        self.assertEqual(self.page.clicks,[])
    def test_click_error_is_not_automatically_repeated(self):
        def fail(x,y):self.page.clicks.append((x,y));raise RuntimeError('unknown native result')
        self.page.mouse.click=fail
        with self.assertRaises(RuntimeError):click_background(self.page,Node([self.good,self.good]))
        self.assertEqual(len(self.page.clicks),1)

    def test_checkpoint_runs_after_guards_and_before_native_input(self):
        observations=[]
        def checkpoint():observations.append(list(self.page.clicks))
        click_background(self.page,Node([self.good,self.good]),before_input=checkpoint)
        self.assertEqual(observations,[[]]);self.assertEqual(len(self.page.clicks),1)

    def test_obstruction_does_not_claim_input_checkpoint(self):
        observations=[]
        with self.assertRaises(PublishError):
            click_background(self.page,Node([dict(self.good,ready=False)]),before_input=lambda:observations.append(True))
        self.assertEqual(observations,[]);self.assertEqual(self.page.clicks,[])

    def test_checkpoint_write_failure_prevents_native_input(self):
        def fail():raise OSError('checkpoint cannot persist')
        with self.assertRaises(OSError):click_background(self.page,Node([self.good,self.good]),before_input=fail)
        self.assertEqual(self.page.clicks,[])


if __name__=='__main__':unittest.main()
