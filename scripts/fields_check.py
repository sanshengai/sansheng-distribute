#!/usr/bin/env python3
"""封存前只读核对各平台填写项（标题/简介/话题/合集/AI声明/原创/封面/活动）。

读取平台文案清单（如 文案-S4.json 的 items，或封存 spec/manifest 的 targets）
与 assets/platform-fields.json 的平台字段矩阵，报告缺项并显式列出该路线不支持的字段。
不连接平台、不写账本、不改输入文件。退出码：0 齐全，1 有缺项，2 输入错误。
"""
import argparse, json, sys
from pathlib import Path

DEFAULT_FIELDS = Path(__file__).resolve().parent.parent / 'assets' / 'platform-fields.json'
REQUIRED = ('title', 'description', 'topics', 'collection', 'ai_declaration', 'original', 'cover')
OPTIONAL = ('activity',)


def lookup(item, dotted):
    cur = item
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def present(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return True  # 显式 true/false 都是已作出的声明
    if isinstance(value, (str, list, dict)) and not value:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def value_of(item, field, keys):
    for k in keys.get(field, []):
        v = lookup(item, k)
        if present(v):
            return v
    return None


def load_items(data):
    if isinstance(data, list):
        return data
    for k in ('items', 'targets'):
        if isinstance(data, dict) and isinstance(data.get(k), list):
            return data[k]
    raise ValueError('找不到 items 或 targets 列表')


def resolve(matrix, platform):
    name = matrix.get('aliases', {}).get(platform, platform)
    if name not in matrix['platforms']:
        raise ValueError(f'矩阵中没有平台：{platform}')
    return name, matrix['platforms'][name]


def check(items, matrix, platform):
    name, spec = resolve(matrix, platform)
    keys, fields = matrix['field_keys'], spec['fields']
    unsupported = sorted(f for f, m in fields.items() if m == 'unsupported')
    checklist = sorted(f for f, m in fields.items() if m == 'checklist')
    report = dict(platform=name, route=spec.get('route'), unsupported=unsupported,
                  checklist_fields=checklist, items=[], ok=True)
    for it in items:
        rid = it.get('id') or it.get('target_id') or '?'
        missing, warnings = [], []
        for f in REQUIRED + OPTIONAL:
            mode = fields.get(f, 'not_applicable')
            if mode == 'not_applicable':
                continue
            v = value_of(it, f, keys)
            if mode == 'unsupported':
                if v is not None:
                    warnings.append(f'{f}: 已填写但该路线不支持，不会生效')
                continue
            if f in OPTIONAL:
                if v is None:
                    warnings.append(f'{f}: 未设置（可选；不相关则记录未参加）')
                continue
            if v is None:
                missing.append(f)
            elif f == 'ai_declaration' and v is False:
                warnings.append('ai_declaration: 显式声明不含AI/合成内容，请按真实制作来源确认')
            elif f == 'original' and lookup(it, 'platform_metadata.copyright') not in (None, 1):
                warnings.append('original: copyright 不是 1（自制），请确认是否转载')
        for f in unsupported:
            warnings.append(f'{f}: 该路线不支持，需另行补齐或记录缺口')
        if missing:
            report['ok'] = False
        report['items'].append(dict(id=rid, missing=missing, warnings=warnings))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--platform', required=True)
    ap.add_argument('--copy', required=True, help='文案 JSON（items 或 targets）')
    ap.add_argument('--fields', default=str(DEFAULT_FIELDS))
    a = ap.parse_args(argv)
    try:
        matrix = json.loads(Path(a.fields).read_text(encoding='utf-8'))
        items = load_items(json.loads(Path(a.copy).read_text(encoding='utf-8')))
        rep = check(items, matrix, a.platform)
    except (OSError, ValueError, KeyError) as e:
        print(json.dumps(dict(ok=False, error=str(e)), ensure_ascii=False)); return 2
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
