#!/usr/bin/env python3
"""Read/claim author-submitted browser tasks and record observed results locally."""
import argparse
import json
import sys

from interaction_review import ReviewDesk
from publish_core import PublishError


def main():
    p = argparse.ArgumentParser(description='Agent 浏览器接力本地队列；不联网、不操作浏览器')
    p.add_argument('--state-dir', required=True)
    sub = p.add_subparsers(dest='action', required=True)
    sub.add_parser('list')
    for name in ('claim', 'unknown', 'verify'):
        command = sub.add_parser(name)
        command.add_argument('--batch', required=True)
        command.add_argument('--draft', required=True)
        if name == 'verify':
            command.add_argument('--reply-id', required=True)
    args = p.parse_args()
    try:
        desk = ReviewDesk(args.state_dir)
        if args.action == 'list':
            value = {'items': desk.handoffs(), 'browser_actions': 0}
        elif args.action == 'claim':
            value = desk.claim_handoff(args.batch, args.draft)
        else:
            value = desk.finish_handoff(args.batch, args.draft, reply_id=getattr(args, 'reply_id', None))
        print(json.dumps(value, ensure_ascii=False))
    except (PublishError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
