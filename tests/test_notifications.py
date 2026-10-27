import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import requests

from utils.notifications import Notifications
from utils.notifications.apprise import Apprise
from utils.notifications.pushover import Pushover
from utils.notifications.slack import Slack


class FakeWebhook:
    """Records POSTed JSON bodies and answers with a configurable status."""

    def __init__(self, status=200):
        self.status = status
        self.posts = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                fake.posts.append((self.path, dict(self.headers), json.loads(self.rfile.read(length) or b'null')))
                self.send_response(fake.status)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.host = f'127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def fake_webhook(test, status=200):
    fake = FakeWebhook(status)
    test.addCleanup(fake.close)
    return fake


class SlackTests(unittest.TestCase):
    def test_payload_posted_to_webhook(self):
        fake = fake_webhook(self)
        slack = Slack(f'http://{fake.host}/services/T000/B000/XXXX', channel='#media')
        self.assertTrue(slack.send('Upload finished'))
        path, headers, body = fake.posts[0]
        self.assertEqual(path, '/services/T000/B000/XXXX')
        self.assertTrue(headers['Content-Type'].startswith('application/json'))
        self.assertEqual(body, {'text': 'Upload finished', 'username': 'Cloudplow',
                                'icon_emoji': ':heavy_exclamation_mark:', 'channel': '#media'})

    def test_channel_omitted_when_not_set(self):
        fake = fake_webhook(self)
        self.assertTrue(Slack(f'http://{fake.host}/hook', sender_name='bot', sender_icon=':x:').send('m'))
        self.assertEqual(fake.posts[0][2], {'text': 'm', 'username': 'bot', 'icon_emoji': ':x:'})

    def test_error_status_is_failure(self):
        fake = fake_webhook(self, status=404)
        self.assertFalse(Slack(f'http://{fake.host}/hook').send('m'))
        self.assertEqual(len(fake.posts), 1)

    def test_missing_settings_send_nothing(self):
        with mock.patch('utils.notifications.slack.requests.post') as post:
            for slack in (Slack(''), Slack('http://127.0.0.1:9/hook', sender_name=''),
                          Slack('http://127.0.0.1:9/hook', sender_icon='')):
                with self.assertLogs('utils.notifications.slack', 'ERROR'):
                    self.assertFalse(slack.send('m'))
        post.assert_not_called()

    def test_connection_error_is_logged_without_webhook_url(self):
        webhook = 'http://127.0.0.1:9/services/T000/B000/SECRETPART'
        with self.assertLogs('utils.notifications.slack', 'ERROR') as cm:
            self.assertFalse(Slack(webhook).send('m'))
        self.assertEqual(cm.records[0].getMessage(), 'Error sending Slack notification')
        self.assertIsNotNone(cm.records[0].exc_info)


class PushoverTests(unittest.TestCase):
    def test_payload(self):
        with mock.patch('utils.notifications.pushover.requests.post') as post:
            post.return_value.status_code = 200
            self.assertTrue(Pushover('app', 'user', priority=1).send('Upload finished'))
        post.assert_called_once_with('https://api.pushover.net/1/messages.json',
                                     data={'token': 'app', 'user': 'user', 'priority': 1,
                                           'message': 'Upload finished'},
                                     timeout=30)

    def test_rejected_by_api(self):
        with mock.patch('utils.notifications.pushover.requests.post') as post:
            post.return_value.status_code = 400
            self.assertFalse(Pushover('app', 'user').send('m'))

    def test_network_error(self):
        with mock.patch('utils.notifications.pushover.requests.post',
                        side_effect=requests.ConnectionError('down')):
            with self.assertLogs('utils.notifications.pushover', 'ERROR'):
                self.assertFalse(Pushover('app', 'user').send('m'))

    def test_missing_tokens(self):
        with mock.patch('utils.notifications.pushover.requests.post') as post:
            for agent in (Pushover('', 'user'), Pushover('app', '')):
                with self.assertLogs('utils.notifications.pushover', 'ERROR'):
                    self.assertFalse(agent.send('m'))
        post.assert_not_called()


class AppriseTests(unittest.TestCase):
    def test_json_url_delivered_to_local_endpoint(self):
        fake = fake_webhook(self)
        self.assertTrue(Apprise(f'json://{fake.host}/notify', title='Plow').send('Upload finished'))
        path, _, body = fake.posts[0]
        self.assertEqual(path, '/notify')
        self.assertEqual((body['title'], body['message']), ('Plow', 'Upload finished'))

    def test_endpoint_error_is_failure(self):
        fake = fake_webhook(self, status=500)
        self.assertFalse(Apprise(f'json://{fake.host}/notify').send('m'))

    def test_missing_url(self):
        with self.assertLogs('utils.notifications.apprise', 'ERROR'):
            self.assertFalse(Apprise('').send('m'))

    def test_uses_title_and_url(self):
        with mock.patch('utils.notifications.apprise.apprise.Apprise') as cls:
            cls.return_value.notify.return_value = True
            self.assertTrue(Apprise('discord://id/token').send('hello'))
        cls.return_value.add.assert_called_once_with('discord://id/token')
        cls.return_value.notify.assert_called_once_with(title='Cloudplow', body='hello')


class FakeAgent:
    def __init__(self, name, result=True, error=None):
        self.NAME = name
        self.result, self.error = result, error
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        if self.error:
            raise self.error
        return self.result


class NotificationsTests(unittest.TestCase):
    def test_load_known_services(self):
        notifications = Notifications()
        self.assertTrue(notifications.load(service='slack', webhook_url='http://x/hook'))
        self.assertTrue(notifications.load(service='pushover', app_token='a', user_token='u'))
        self.assertTrue(notifications.load(service='apprise', url='json://localhost'))
        self.assertEqual([type(s) for s in notifications.services], [Slack, Pushover, Apprise])

    def test_load_rejects_unknown_service_and_bad_settings(self):
        notifications = Notifications()
        with self.assertLogs('utils.notifications', 'ERROR') as cm:
            self.assertFalse(notifications.load(service='email', to='me'))
            self.assertFalse(notifications.load())
            # config.json entries carry a 'service' key; anything unexpected must not crash startup
            self.assertFalse(notifications.load(service='slack', webhook_url='x', bogus=1))
            self.assertFalse(notifications.load(service='pushover'))
        self.assertEqual(notifications.services, [])
        self.assertIn('invalid notification service', cm.output[0])

    def test_send_fans_out_and_survives_failing_agents(self):
        notifications = Notifications()
        ok, failing, broken = FakeAgent('Slack'), FakeAgent('Pushover', result=False), \
            FakeAgent('Apprise', error=RuntimeError('boom'))
        notifications.services = [broken, failing, ok]
        with self.assertLogs('utils.notifications', 'INFO') as cm:
            notifications.send('Upload done')
        for agent in (ok, failing, broken):
            self.assertEqual(agent.messages, ['Upload done'])
        output = '\n'.join(cm.output)
        self.assertIn('Exception sending notification with Apprise', output)
        self.assertIn('Sent notification with Slack', output)
        self.assertNotIn('Sent notification with Pushover', output)

    def test_send_to_one_service(self):
        notifications = Notifications()
        slack, pushover = FakeAgent('Slack'), FakeAgent('Pushover')
        notifications.services = [slack, pushover]
        with self.assertLogs('utils.notifications', 'INFO'):
            notifications.send('only pushover', service='PUSHOVER')
        self.assertEqual((slack.messages, pushover.messages), ([], ['only pushover']))

    def test_end_to_end_with_real_agents(self):
        fake = fake_webhook(self)
        notifications = Notifications()
        notifications.load(service='slack', webhook_url=f'http://{fake.host}/slack')
        notifications.load(service='apprise', url=f'json://{fake.host}/apprise')
        notifications.send('hello')
        self.assertEqual(sorted(p[0] for p in fake.posts), ['/apprise', '/slack'])


if __name__ == '__main__':
    unittest.main()
