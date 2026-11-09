(function () {
  'use strict';

  var PRIORITY_LABELS = { 1: '低', 2: '中', 3: '高' };
  var VALID_FILTERS = ['all', 'true', 'false'];

  var form = document.getElementById('task-form');
  var titleInput = document.getElementById('title-input');
  var prioritySelect = document.getElementById('priority-select');
  var addButton = document.getElementById('add-button');
  var formError = document.getElementById('form-error');
  var saveStatus = document.getElementById('save-status');
  var taskList = document.getElementById('task-list');
  var emptyHint = document.getElementById('empty-hint');
  var filterButtons = Array.prototype.slice.call(document.querySelectorAll('.filter'));

  var currentFilter = 'all';
  var loading = false;

  function showError(message) {
    formError.textContent = message;
    formError.hidden = false;
  }

  function clearError() {
    formError.textContent = '';
    formError.hidden = true;
  }

  function setStatus(message, isError) {
    saveStatus.textContent = message;
    if (isError) {
      saveStatus.classList.add('is-error');
    } else {
      saveStatus.classList.remove('is-error');
    }
  }

  function setBusy(busy) {
    loading = busy;
    addButton.disabled = busy;
    filterButtons.forEach(function (btn) {
      btn.disabled = busy;
    });
  }

  function readJson(response) {
    return response.json().catch(function () {
      return null;
    });
  }

  function errorMessageFrom(data, fallback) {
    if (data && typeof data.error === 'string' && data.error) {
      return data.error;
    }
    return fallback;
  }

  function renderTasks(tasks) {
    taskList.textContent = '';
    tasks.forEach(function (task) {
      var item = document.createElement('li');
      item.className = 'task-item' + (task.done ? ' is-done' : '');
      item.setAttribute('data-id', String(task.id));

      var label = document.createElement('label');
      label.className = 'task-toggle';

      var checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = task.done === true;
      checkbox.setAttribute('aria-label', '标记完成：' + task.title);
      checkbox.addEventListener('change', function () {
        toggleTask(task, checkbox);
      });

      var title = document.createElement('span');
      title.className = 'task-title';
      title.textContent = task.title;

      label.appendChild(checkbox);
      label.appendChild(title);

      var badge = document.createElement('span');
      badge.className = 'priority-badge priority-' + String(task.priority);
      badge.textContent = '优先级：' + (PRIORITY_LABELS[task.priority] || String(task.priority));

      item.appendChild(label);
      item.appendChild(badge);
      taskList.appendChild(item);
    });
    emptyHint.hidden = tasks.length > 0;
  }

  function loadTasks() {
    if (loading) {
      return;
    }
    setBusy(true);
    setStatus('加载中…', false);
    var url = '/api/tasks?done=' + encodeURIComponent(currentFilter);
    fetch(url, { method: 'GET', headers: { Accept: 'application/json' } })
      .then(function (response) {
        return readJson(response).then(function (data) {
          if (!response.ok) {
            throw new Error(errorMessageFrom(data, '加载失败（HTTP ' + response.status + '）'));
          }
          if (!Array.isArray(data)) {
            throw new Error('加载失败：返回数据格式不正确');
          }
          return data;
        });
      })
      .then(function (tasks) {
        renderTasks(tasks);
        setStatus('已加载', false);
      })
      .catch(function (err) {
        setStatus('加载失败', true);
        showError(err && err.message ? err.message : '加载失败');
      })
      .then(function () {
        setBusy(false);
      });
  }

  function validateForm() {
    var title = titleInput.value.trim();
    if (title.length < 1 || title.length > 80) {
      return { error: '标题去除两端空白后需为 1 至 80 个字符' };
    }
    var priority = Number(prioritySelect.value);
    if (priority !== 1 && priority !== 2 && priority !== 3) {
      return { error: '请选择有效的优先级' };
    }
    return { title: title, priority: priority };
  }

  function createTask(event) {
    event.preventDefault();
    if (loading) {
      return;
    }
    clearError();
    var result = validateForm();
    if (result.error) {
      showError(result.error);
      setStatus('保存失败', true);
      return;
    }

    setBusy(true);
    setStatus('保存中…', false);
    fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ title: result.title, priority: result.priority })
    })
      .then(function (response) {
        return readJson(response).then(function (data) {
          if (!response.ok) {
            throw new Error(errorMessageFrom(data, '保存失败（HTTP ' + response.status + '）'));
          }
          return data;
        });
      })
      .then(function () {
        titleInput.value = '';
        setStatus('已保存', false);
        setBusy(false);
        loadTasks();
      })
      .catch(function (err) {
        setStatus('保存失败', true);
        showError(err && err.message ? err.message : '保存失败');
        setBusy(false);
      });
  }

  function toggleTask(task, checkbox) {
    if (loading) {
      checkbox.checked = task.done === true;
      return;
    }
    clearError();
    setBusy(true);
    setStatus('保存中…', false);
    fetch('/api/tasks/' + encodeURIComponent(String(task.id)), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ done: !task.done })
    })
      .then(function (response) {
        return readJson(response).then(function (data) {
          if (!response.ok) {
            throw new Error(errorMessageFrom(data, '保存失败（HTTP ' + response.status + '）'));
          }
          return data;
        });
      })
      .then(function () {
        setStatus('已保存', false);
        setBusy(false);
        loadTasks();
      })
      .catch(function (err) {
        checkbox.checked = task.done === true;
        setStatus('保存失败', true);
        showError(err && err.message ? err.message : '保存失败');
        setBusy(false);
      });
  }

  function selectFilter(button) {
    var value = button.getAttribute('data-filter');
    if (VALID_FILTERS.indexOf(value) === -1 || value === currentFilter) {
      return;
    }
    currentFilter = value;
    filterButtons.forEach(function (btn) {
      var active = btn === button;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    clearError();
    loadTasks();
  }

  form.addEventListener('submit', createTask);
  titleInput.addEventListener('input', function () {
    if (!formError.hidden) {
      clearError();
    }
  });
  filterButtons.forEach(function (btn) {
    btn.setAttribute('aria-pressed', btn.classList.contains('active') ? 'true' : 'false');
    btn.addEventListener('click', function () {
      selectFilter(btn);
    });
  });

  loadTasks();
})();
