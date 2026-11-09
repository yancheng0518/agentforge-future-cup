"""Maintainer-frozen HTTP checks; never execute candidate on the host."""
import unittest
from web_bridge import request


class BoardDeliveryTests(unittest.TestCase):
    def add(self, title='External check', priority=2):
        response = request('POST', '/api/tasks', json={'title': title, 'priority': priority})
        self.assertEqual(response['status'], 201)
        return response['json']

    def test_health(self):
        self.assertEqual(request('GET', '/health')['json'], {'status': 'ok'})

    def test_html_and_local_assets(self):
        response = request('GET', '/')
        self.assertEqual(response['status'], 200)
        self.assertIn('text/html', response['headers'].get('content-type', ''))
        self.assertIn('工作项', response['text'])
        self.assertNotIn('https://', response['text'])
        self.assertNotIn('http://', response['text'])

    def test_create_trim_and_boolean(self):
        item = self.add('  Test trim  ', 3)
        self.assertEqual(item['title'], 'Test trim')
        self.assertEqual(item['priority'], 3)
        self.assertIs(item['done'], False)
        self.assertIs(type(item['id']), int)
        self.assertGreater(item['id'], 0)

    def test_monotonic_ids(self):
        first, second = self.add('One'), self.add('Two')
        self.assertGreater(second['id'], first['id'])

    def test_order_and_ties(self):
        first, second = self.add('Tie A', 2), self.add('Tie B', 2)
        response = request('GET', '/api/tasks')
        self.assertEqual(response['status'], 200)
        items = response['json']
        keys = [(-item['priority'], item['id']) for item in items]
        self.assertEqual(keys, sorted(keys))
        ids = [item['id'] for item in items]
        self.assertLess(ids.index(first['id']), ids.index(second['id']))

    def test_done_filter(self):
        active, done = self.add('Active filter'), self.add('Done filter')
        response = request('PATCH', '/api/tasks/' + str(done['id']), json={'done': True})
        self.assertEqual(response['status'], 200)
        self.assertIs(response['json']['done'], True)
        done_items = request('GET', '/api/tasks?done=true')['json']
        active_items = request('GET', '/api/tasks?done=false')['json']
        self.assertIn(done['id'], [item['id'] for item in done_items])
        self.assertNotIn(active['id'], [item['id'] for item in done_items])
        self.assertIn(active['id'], [item['id'] for item in active_items])
        self.assertNotIn(done['id'], [item['id'] for item in active_items])

    def test_invalid_title(self):
        for title in ['', '  ', 'x' * 81, None, 7]:
            with self.subTest(title=title):
                result = request('POST', '/api/tasks', json={'title': title, 'priority': 1})
                self.assertEqual(result['status'], 400)
                self.assertIn('error', result['json'])

    def test_invalid_priority(self):
        for priority in [True, False, '2', 2.5, 0, 4, None]:
            with self.subTest(priority=priority):
                self.assertEqual(request('POST', '/api/tasks', json={'title': 'Invalid priority', 'priority': priority})['status'], 400)

    def test_post_shape(self):
        for body in [[], None, {}, {'title': 'No priority'}, {'title': 'Extra', 'priority': 1, 'extra': 0}]:
            with self.subTest(body=body):
                response = request('POST', '/api/tasks', json=body)
                self.assertEqual(response['status'], 400)
                self.assertIn('error', response['json'])

    def test_patch_shape_and_atomicity(self):
        item = self.add('Unchanged task', 1)
        for body in [{'done': 1}, {'done': 'true'}, {}, {'done': True, 'title': 'Changed'}]:
            with self.subTest(body=body):
                self.assertEqual(request('PATCH', '/api/tasks/' + str(item['id']), json=body)['status'], 400)
        found = next(row for row in request('GET', '/api/tasks')['json'] if row['id'] == item['id'])
        self.assertEqual(found, item)

    def test_missing_id_json_error(self):
        response = request('PATCH', '/api/tasks/999999', json={'done': True})
        self.assertEqual(response['status'], 404)
        self.assertIn('error', response['json'])

    def test_invalid_filter(self):
        response = request('GET', '/api/tasks?done=maybe')
        self.assertEqual(response['status'], 400)
        self.assertIn('error', response['json'])
