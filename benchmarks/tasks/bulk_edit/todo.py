"""Task baseline: completion works; requested bulk editing is not implemented.

This maintainer-provided starting project is not a live-model output or an
injected defect in a model-generated candidate.
"""


class TodoStore:
    def __init__(self):
        self._tasks = []
        self._next_id = 1

    def add(self, title):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must not be empty")
        task = {"id": self._next_id, "title": title.strip(), "completed": False}
        self._next_id += 1
        self._tasks.append(task)
        return dict(task)

    def complete(self, task_id, completed=True):
        for task in self._tasks:
            if task["id"] == task_id:
                task["completed"] = bool(completed)
                return dict(task)
        raise KeyError(task_id)

    def list(self, completed=None):
        return [dict(task) for task in self._tasks
                if completed is None or task["completed"] == completed]
