"""Private immutable upload snapshots, shared by bundled browser engines."""
from __future__ import annotations
import copy
import hashlib
import os
from pathlib import Path
import shutil
from publish_core import PublishError, file_hash


def stage(pack, request_path):
    root = Path(request_path).with_suffix('.browser-assets')
    root.mkdir(mode=0o700, exist_ok=False)
    staged = copy.deepcopy(pack['assets'])
    try:
        for asset in staged:
            source = Path(asset['absolute_path'])
            destination = root / (asset['id'] + source.suffix.lower())
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            actual = hashlib.sha256()
            with os.fdopen(fd, 'wb') as outgoing, source.open('rb') as incoming:
                while block := incoming.read(1024 * 1024):
                    actual.update(block); outgoing.write(block)
                outgoing.flush(); os.fsync(outgoing.fileno())
            if actual.hexdigest() != asset['sha256']:
                raise PublishError('浏览器素材在封存后改变；未上传，请交回制作端重新封存')
            destination.chmod(0o400)
            asset['absolute_path'] = str(destination)
    except Exception:
        shutil.rmtree(root)
        raise
    return staged


def checked_path(asset):
    path = Path(asset['absolute_path'])
    if not path.is_file() or file_hash(path) != asset['sha256']:
        raise PublishError('私有上传快照缺失或改变；不继续上传')
    return str(path)
