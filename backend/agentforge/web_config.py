"""Frozen Web runtime configuration. Source files cannot change dependencies."""
from __future__ import annotations

import hashlib
import json
import re

from .runtime import RuntimeErrorSafe

WEB_FIELDS = {'schema', 'entrypoint', 'dependency_id', 'image_id', 'lock_sha256', 'requirements'}


def web_runtime_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_web_config(value, files=None):
    if not isinstance(value, dict) or set(value) != WEB_FIELDS or value.get('schema') != 'python-web-v1':
        raise RuntimeErrorSafe('Web 配置需要冻结入口、依赖 bundle、锁和镜像 ID。')
    value = json.loads(json.dumps(value))
    if (not isinstance(value['entrypoint'], str)
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_/]*\.py', value['entrypoint'])
            or any(part in ('', '.', '..') for part in value['entrypoint'].split('/'))
            or files is not None and value['entrypoint'] not in files):
        raise RuntimeErrorSafe('Web 入口必须是候选项目内的 Python 源文件。')
    if any(not isinstance(value[key], str) or not re.fullmatch(r'[a-f0-9]{64}', value[key])
           for key in ('dependency_id', 'lock_sha256')):
        raise RuntimeErrorSafe('Web 依赖 ID 和锁 SHA-256 无效。')
    if not isinstance(value['image_id'], str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', value['image_id']):
        raise RuntimeErrorSafe('Web 运行必须固定 Docker 镜像 ID。')
    required = value['requirements']
    if (not isinstance(required, list) or not 1 <= len(required) <= 16
            or any(not isinstance(item, str) or len(item) > 160
                   or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+-]*', item)
                   for item in required)):
        raise RuntimeErrorSafe('Web 依赖必须是固定版本的 PyPI 包清单。')
    return value
