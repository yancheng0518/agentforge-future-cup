"""Maintainer-written import baseline; run only in AgentForge Docker."""
import os
from flask import Flask, jsonify

app = Flask(__name__)


@app.get('/health')
def health():
    return jsonify({'status': 'ok'})


@app.get('/')
def index():
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>工作项看板</title><h1>等待开发工作项看板</h1></html>'


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8080')), debug=False, use_reloader=False)
