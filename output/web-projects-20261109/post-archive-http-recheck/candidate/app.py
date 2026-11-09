"""Maintainer-written import baseline; run only in AgentForge Docker."""
import os
from flask import Flask, jsonify, request

app = Flask(__name__)

# 进程内任务存储：id -> task dict
tasks = {}
next_id = 1

INDEX_HTML = '''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>工作项看板</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<main class="board">
  <h1>工作项看板</h1>
  <form id="task-form" class="task-form" novalidate>
    <label class="field">
      <span>标题</span>
      <input id="title-input" type="text" maxlength="80" placeholder="输入工作项标题" autocomplete="off">
    </label>
    <label class="field">
      <span>优先级</span>
      <select id="priority-select">
        <option value="3">高</option>
        <option value="2" selected>中</option>
        <option value="1">低</option>
      </select>
    </label>
    <button id="add-button" type="submit">新增</button>
  </form>
  <p id="form-error" class="error" role="alert" hidden></p>
  <div class="toolbar">
    <div class="filters" role="group" aria-label="筛选">
      <button type="button" class="filter active" data-filter="all">全部</button>
      <button type="button" class="filter" data-filter="false">未完成</button>
      <button type="button" class="filter" data-filter="true">已完成</button>
    </div>
    <span id="save-status" class="status" aria-live="polite"></span>
  </div>
  <ul id="task-list" class="task-list"></ul>
  <p id="empty-hint" class="empty" hidden>暂无工作项</p>
</main>
<script src="/static/app.js"></script>
</body>
</html>'''


def error_response(message, status):
    return jsonify({'error': message}), status


def task_payload(task):
    return {
        'id': task['id'],
        'title': task['title'],
        'priority': task['priority'],
        'done': bool(task['done']),
    }


@app.get('/health')
def health():
    return jsonify({'status': 'ok'})


@app.get('/')
def index():
    return INDEX_HTML


@app.post('/api/tasks')
def create_task():
    global next_id
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response('请求体必须是 JSON 对象', 400)
    if set(data.keys()) != {'title', 'priority'}:
        return error_response('字段必须恰好为 title 和 priority', 400)

    title = data['title']
    if not isinstance(title, str):
        return error_response('title 必须是字符串', 400)
    title = title.strip()
    if not (1 <= len(title) <= 80):
        return error_response('title 去除两端空白后长度必须为 1 至 80 字符', 400)

    priority = data['priority']
    if isinstance(priority, bool) or not isinstance(priority, int):
        return error_response('priority 必须是整数 1、2 或 3', 400)
    if priority not in (1, 2, 3):
        return error_response('priority 必须是整数 1、2 或 3', 400)

    task = {
        'id': next_id,
        'title': title,
        'priority': priority,
        'done': False,
    }
    tasks[next_id] = task
    next_id += 1
    return jsonify(task_payload(task)), 201


@app.get('/api/tasks')
def list_tasks():
    done_filter = request.args.get('done', 'all')
    if done_filter not in ('all', 'true', 'false'):
        return error_response('done 参数仅接受 all、true 或 false', 400)

    items = list(tasks.values())
    if done_filter == 'true':
        items = [t for t in items if t['done']]
    elif done_filter == 'false':
        items = [t for t in items if not t['done']]

    items.sort(key=lambda t: (-t['priority'], t['id']))
    return jsonify([task_payload(t) for t in items])


@app.patch('/api/tasks/<task_id>')
def update_task(task_id):
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return error_response('工作项不存在', 404)

    task = tasks.get(tid)
    if task is None:
        return error_response('工作项不存在', 404)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response('请求体必须是 JSON 对象', 400)
    if set(data.keys()) != {'done'}:
        return error_response('字段必须恰好为 done', 400)
    done = data['done']
    if not isinstance(done, bool):
        return error_response('done 必须是布尔值', 400)

    task['done'] = done
    return jsonify(task_payload(task)), 200


@app.errorhandler(404)
def handle_404(err):
    if request.path.startswith('/api/'):
        return error_response('接口不存在', 404)
    return err


@app.errorhandler(405)
def handle_405(err):
    if request.path.startswith('/api/'):
        return error_response('方法不允许', 405)
    return err


@app.errorhandler(500)
def handle_500(err):
    if request.path.startswith('/api/'):
        return error_response('服务器内部错误', 500)
    return err


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8080')), debug=False, use_reloader=False)
