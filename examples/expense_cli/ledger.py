"""Initial working project for a declared project-continuation experiment."""


def add_expense(entries, title, cents, category):
    if not isinstance(title, str) or not title.strip():
        raise ValueError('title must not be empty')
    if type(cents) is not int or cents <= 0:
        raise ValueError('cents must be a positive integer')
    if not isinstance(category, str) or not category.strip():
        raise ValueError('category must not be empty')
    result = [dict(item) for item in entries]
    result.append({'id': max((item['id'] for item in result), default=0) + 1,
                   'title': title.strip(), 'cents': cents, 'category': category.strip()})
    return result


def list_expenses(entries):
    return sorted((dict(item) for item in entries), key=lambda item: item['id'])
