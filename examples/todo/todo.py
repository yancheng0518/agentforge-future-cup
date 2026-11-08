"""Small, deliberately incomplete example project; candidate edits go to copies."""


class TodoStore:
    def __init__(self):
        self._tasks = []
        self._next_id = 1

    def add(self, title):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must not be empty")
        task = {"id": self._next_id, "title": title.strip()}
        self._next_id += 1
        self._tasks.append(task)
        return dict(task)

    def list(self):
        return [dict(task) for task in self._tasks]
