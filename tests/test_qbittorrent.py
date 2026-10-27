import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

from cloudplow import Cloudplow
from utils.qbittorrent import Qbittorrent


class FakeQbittorrent:
    """Just enough of the qBittorrent Web API to exercise the client over real HTTP."""

    def __init__(self, username='admin', password='secret', alt_limits=False):
        self.username, self.password = username, password
        self.alt_limits = alt_limits
        self.calls = []
        # failure switches
        self.toggle_status = 200
        self.mode_body = None
        self.login_status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                url = urlparse(self.path)
                length = int(self.headers.get('Content-Length') or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                fake.calls.append(url.path)
                status, body, headers = fake.respond(url.path, form, self.headers)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body.encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, path, form, headers):
        if path == '/api/v2/auth/login':
            if self.login_status != 200:
                return self.login_status, 'error', {}
            if headers.get('Referer', '').rstrip('/') != self.url:
                return 403, 'Forbidden', {}  # qBittorrent's CSRF protection
            if (form.get('username'), form.get('password')) == (self.username, self.password):
                return 200, 'Ok.', {'Set-Cookie': 'SID=abc; path=/'}
            return 200, 'Fails.', {}
        if self.username and 'SID=abc' not in headers.get('Cookie', ''):
            return 403, 'Forbidden', {}
        if path == '/api/v2/transfer/speedLimitsMode':
            if self.mode_body is not None:
                return 200, self.mode_body, {}
            return 200, '1' if self.alt_limits else '0', {}
        if path == '/api/v2/transfer/toggleSpeedLimitsMode':
            if self.toggle_status != 200:
                return self.toggle_status, 'error', {}
            self.alt_limits = not self.alt_limits
            return 200, '', {}
        return 404, 'Not Found', {}


class QbittorrentTests(unittest.TestCase):
    def make(self, **kwargs):
        fake = FakeQbittorrent(**kwargs)
        self.addCleanup(fake.close)
        return fake

    def test_limits_enabled_for_the_upload_then_restored(self):
        fake = self.make()
        state = {}
        client = Qbittorrent(fake.url, 'admin', 'secret', state)
        self.assertTrue(client.pause_queue())
        self.assertTrue(fake.alt_limits)
        self.assertTrue(state['alt_limits_enabled'])
        self.assertTrue(client.resume_queue())
        self.assertFalse(fake.alt_limits)
        self.assertNotIn('alt_limits_enabled', state)

    def test_limits_already_on_are_left_on(self):
        fake = self.make(alt_limits=True)
        state = {}
        client = Qbittorrent(fake.url, 'admin', 'secret', state)
        self.assertTrue(client.pause_queue())
        self.assertTrue(client.resume_queue())
        self.assertTrue(fake.alt_limits)
        self.assertNotIn('/api/v2/transfer/toggleSpeedLimitsMode', fake.calls)

    def test_user_turning_limits_off_mid_upload_is_respected(self):
        fake = self.make()
        client = Qbittorrent(fake.url, 'admin', 'secret', {})
        client.pause_queue()
        fake.alt_limits = False   # switched off in the web UI while uploading
        self.assertTrue(client.resume_queue())
        self.assertFalse(fake.alt_limits)   # not toggled back on

    def test_wrong_password_fails_without_changes(self):
        fake = self.make()
        self.assertFalse(Qbittorrent(fake.url, 'admin', 'wrong', {}).pause_queue())
        self.assertFalse(fake.alt_limits)

    def test_no_credentials_for_auth_bypass(self):
        fake = self.make(username='')
        self.assertTrue(Qbittorrent(fake.url, state={}).pause_queue())
        self.assertTrue(fake.alt_limits)
        self.assertNotIn('/api/v2/auth/login', fake.calls)

    def test_unreachable_server_fails(self):
        self.assertFalse(Qbittorrent('http://127.0.0.1:9', state={}).pause_queue())

    def test_failed_toggle_is_not_recorded_as_ours(self):
        fake = self.make()
        fake.toggle_status = 500
        state = {}
        with self.assertLogs('utils.qbittorrent', 'ERROR'):
            self.assertFalse(Qbittorrent(fake.url, 'admin', 'secret', state).pause_queue())
        self.assertNotIn('alt_limits_enabled', state)   # nothing to undo later

    def test_failed_restore_keeps_the_record_for_a_retry(self):
        fake = self.make()
        state = {}
        client = Qbittorrent(fake.url, 'admin', 'secret', state)
        client.pause_queue()
        fake.toggle_status = 500
        with self.assertLogs('utils.qbittorrent', 'ERROR'):
            self.assertFalse(client.resume_queue())
        self.assertTrue(state['alt_limits_enabled'])
        fake.toggle_status = 200
        self.assertTrue(client.resume_queue())
        self.assertFalse(fake.alt_limits)

    def test_unknown_mode_changes_nothing(self):
        fake = self.make()
        state = {}
        client = Qbittorrent(fake.url, 'admin', 'secret', state)
        client.pause_queue()
        fake.mode_body = 'maybe'
        with self.assertLogs('utils.qbittorrent', 'ERROR'):
            self.assertFalse(client.resume_queue())
            self.assertFalse(Qbittorrent(fake.url, 'admin', 'secret', {}).pause_queue())
        self.assertTrue(state['alt_limits_enabled'])
        self.assertEqual(fake.calls.count('/api/v2/transfer/toggleSpeedLimitsMode'), 1)

    def test_login_error_fails_cleanly(self):
        fake = self.make()
        fake.login_status = 500
        with self.assertLogs('utils.qbittorrent', 'ERROR'):
            self.assertFalse(Qbittorrent(fake.url, 'admin', 'secret', {}).pause_queue())
        self.assertFalse(fake.alt_limits)

    def test_limits_left_on_by_a_restart_are_restored(self):
        fake = self.make()
        state = {}
        Qbittorrent(fake.url, 'admin', 'secret', state).pause_queue()   # cloudplow stops mid-upload here

        restarted = Qbittorrent(fake.url, 'admin', 'secret', state)
        self.assertTrue(restarted.restore_leftovers())
        self.assertFalse(fake.alt_limits)
        self.assertTrue(restarted.restore_leftovers())   # nothing left: a no-op
        self.assertEqual(fake.calls.count('/api/v2/transfer/toggleSpeedLimitsMode'), 2)


class DownloadClientIntegrationTests(unittest.TestCase):
    def test_upload_enables_and_restores_qbittorrent_limits(self):
        fake = FakeQbittorrent()
        self.addCleanup(fake.close)
        plow = object.__new__(Cloudplow)
        plow.configs = {'nzbget': {'enabled': False}, 'sabnzbd': {'enabled': False},
                        'qbittorrent': {'enabled': True, 'url': fake.url, 'username': 'admin', 'password': 'secret'}}
        plow.download_client_state = {}
        plow._log_and_notify = mock.Mock()

        with plow._download_clients_paused():
            self.assertTrue(fake.alt_limits)
        self.assertFalse(fake.alt_limits)

    def test_missing_section_in_older_configs(self):
        plow = object.__new__(Cloudplow)
        plow.configs = {'nzbget': {'enabled': False}, 'sabnzbd': {'enabled': False}}
        plow.download_client_state = {}
        with plow._download_clients_paused():
            pass


if __name__ == '__main__':
    unittest.main()
