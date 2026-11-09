"""Trusted bounded Docker-only HTTP transport; never connects outside loopback."""
import base64
import http.client
import json
import re
import sys
from urllib.parse import unquote, urlsplit

MAX_BODY = 16384
MAX_RESPONSE = 65536
METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}


def request(value):
    method, path = value.get('method'), value.get('path')
    if method not in METHODS or not isinstance(path, str) or len(path) > 4096:
        raise ValueError('Invalid method/path')
    decoded = path
    for _ in range(4):
        decoded = unquote(decoded)
    parts = urlsplit(path)
    if (not path.startswith('/') or path.startswith('//') or parts.scheme or parts.netloc or parts.fragment
            or '\\' in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(part in ('.', '..') for part in decoded.split('?', 1)[0].split('/'))):
        raise ValueError('Only relative local paths are supported')
    headers = value.get('headers', {})
    if not isinstance(headers, dict) or len(headers) > 3:
        raise ValueError('Invalid headers')
    for key, content in headers.items():
        if (key.lower() not in ('accept', 'content-type') or not isinstance(content, str)
                or len(content) > 200 or any(ord(c) < 32 or ord(c) > 126 for c in content)):
            raise ValueError('Unsupported request header')
    body = base64.b64decode(value.get('body_base64', ''), validate=True)
    if len(body) > MAX_BODY:
        raise ValueError('Request body too large')
    headers = {**headers, 'Host': '127.0.0.1:8080', 'Connection': 'close', 'Accept-Encoding': 'identity'}
    connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read(MAX_RESPONSE + 1)
        if len(content) > MAX_RESPONSE:
            raise ValueError('Response body too large')
        result_headers = {}
        for key, value in response.getheaders():
            if key.lower() in ('content-type', 'location') and len(value) <= 2048:
                result_headers[key.lower()] = value
        return {'ok': True, 'status': response.status, 'headers': result_headers,
                'body_base64': base64.b64encode(content).decode('ascii')}
    finally:
        connection.close()


def main():
    nonce = None
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > 40000:
            raise ValueError('Expected a bounded encoded request')
        value = json.loads(base64.b64decode(sys.argv[1], validate=True))
        nonce = value.get('nonce')
        if not isinstance(nonce, str) or not re.fullmatch(r'[a-f0-9]{32}', nonce):
            raise ValueError('Invalid request nonce')
        result = request(value)
    except (ValueError, TypeError, KeyError, UnicodeError, OSError, http.client.HTTPException) as error:
        result = {'ok': False, 'error': type(error).__name__}
    print(json.dumps({**result, 'nonce': nonce}, ensure_ascii=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
