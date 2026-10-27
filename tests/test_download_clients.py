import base64
import json
import logging
import socket
import socketserver
import threading
import time
import unittest
import xmlrpc.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

from utils import logs, xmlrpc as cp_xmlrpc
from utils.nzbget import Nzbget
from utils.sabnzbd import Sabnzbd


def formatted(records):
    """Render captured records through the production formatter, tracebacks included."""
    formatter = logs.RedactingFormatter('%(message)s')
    return '\n'.join(formatter.format(r) for r in records)


class _ThreadingXMLRPCServer(socketserver.ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = True
    block_on_close = False


class FakeNzbget:
    """NZBGet's XML-RPC API on /xmlrpc with HTTP basic auth, served over 127.0.0.1."""

    def __init__(self, username='nzbget', password='tegbzn6789', delay=0):
        self.paused = False
        self.auth_headers = []
        self.fail_with = None
        self.delay = delay
        fake = self

        class Handler(SimpleXMLRPCRequestHandler):
            rpc_paths = ('/xmlrpc',)

            def log_message(self, *args):
                pass

            def do_POST(self):
                auth = self.headers.get('Authorization', '')
                fake.auth_headers.append(auth)
                expected = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
                if username and auth != expected:
                    self.send_response(401)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                super().do_POST()

        self.server = _ThreadingXMLRPCServer(('127.0.0.1', 0), requestHandler=Handler, logRequests=False,
                                             allow_none=True)
        self.server.register_function(self.pausedownload, 'pausedownload')
        self.server.register_function(self.resumedownload, 'resumedownload')
        self.host = f'127.0.0.1:{self.server.server_address[1]}'
        self.url = f'http://{username}:{password}@{self.host}' if username else f'http://{self.host}'
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _act(self, paused):
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        self.paused = paused
        return True

    def pausedownload(self):
        return self._act(True)

    def resumedownload(self):
        return self._act(False)


class NzbgetTests(unittest.TestCase):
    def make(self, **kwargs):
        fake = FakeNzbget(**kwargs)
        self.addCleanup(fake.close)
        return fake

    def test_pause_and_resume_with_basic_auth(self):
        fake = self.make()
        client = Nzbget(fake.url + '/')
        self.assertEqual(client.url, fake.url + '/xmlrpc')
        self.assertTrue(client.pause_queue())
        self.assertTrue(fake.paused)
        self.assertTrue(client.resume_queue())
        self.assertFalse(fake.paused)
        self.assertTrue(fake.auth_headers[0].startswith('Basic '))

    def test_without_credentials(self):
        fake = self.make(username='')
        self.assertTrue(Nzbget(fake.url).pause_queue())
        self.assertEqual(fake.auth_headers, [''])

    def test_rpc_fault_is_failure(self):
        fake = self.make()
        fake.fail_with = ValueError('queue locked')
        with self.assertLogs('utils.nzbget', 'ERROR') as cm:
            self.assertFalse(Nzbget(fake.url).pause_queue())
        self.assertIn('pausedownload', cm.output[0])
        self.assertIsInstance(cm.records[0].exc_info[1], xmlrpc.client.Fault)

    def test_false_result_is_failure(self):
        fake = self.make()
        fake.pausedownload = lambda: False
        fake.server.register_function(fake.pausedownload, 'pausedownload')
        self.assertFalse(Nzbget(fake.url).pause_queue())

    def test_wrong_password_is_failure(self):
        fake = self.make()
        with self.assertLogs('utils.nzbget', 'ERROR') as cm:
            self.assertFalse(Nzbget(f'http://nzbget:wrongpass99@{fake.host}').resume_queue())
        self.assertIn('HTTP 401', cm.output[0])

    def test_password_not_logged_on_http_error(self):
        fake = self.make()
        with self.assertLogs('utils.nzbget', 'ERROR') as cm:
            Nzbget(f'http://nzbget:wrongpass99@{fake.host}').resume_queue()
        self.assertNotIn('wrongpass99', formatted(cm.records))

    def test_password_not_logged_when_unreachable(self):
        with self.assertLogs('utils.nzbget', 'ERROR') as cm:
            self.assertFalse(Nzbget('http://nzbget:hunter2pass@127.0.0.1:9').pause_queue())
        self.assertIsInstance(cm.records[0].exc_info[1], OSError)
        self.assertNotIn('hunter2pass', formatted(cm.records))


class ServerProxyTests(unittest.TestCase):
    def test_transport_matches_scheme(self):
        self.assertIsInstance(cp_xmlrpc.server_proxy('http://h/x')._ServerProxy__transport,
                              cp_xmlrpc._TimeoutTransport)
        self.assertIsInstance(cp_xmlrpc.server_proxy('HTTPS://h/x')._ServerProxy__transport,
                              cp_xmlrpc._TimeoutSafeTransport)

    def test_connections_get_the_timeout(self):
        for transport in (cp_xmlrpc._TimeoutTransport(4), cp_xmlrpc._TimeoutSafeTransport(7)):
            connection = transport.make_connection('127.0.0.1:1')
            self.assertEqual(connection.timeout, transport._timeout)

    def test_slow_server_times_out_without_changing_global_default(self):
        fake = FakeNzbget(username='', delay=1)
        self.addCleanup(fake.close)
        with self.assertRaises(OSError):
            with cp_xmlrpc.server_proxy(f'{fake.url}/xmlrpc', timeout=0.2) as proxy:
                proxy.pausedownload()
        self.assertIsNone(socket.getdefaulttimeout())


class FakeSabnzbd:
    def __init__(self, apikey='sabAPIkey0123456789'):
        self.apikey = apikey
        self.paused = False
        self.requests = []
        self.override = None   # (status, body) to send instead
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append((url.path, params))
                status, body = fake.respond(url.path, params)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, path, params):
        if self.override:
            return self.override
        if path != '/api':
            return 404, b'Not found'
        if params.get('apikey') != self.apikey:
            return 200, json.dumps({'status': False, 'error': 'API Key Incorrect'}).encode()
        if params.get('mode') in ('pause', 'resume'):
            self.paused = params['mode'] == 'pause'
            return 200, b'{"status": true}'
        return 200, json.dumps({'status': False, 'error': 'not implemented'}).encode()


class SabnzbdTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSabnzbd()
        self.addCleanup(self.fake.close)

    def test_pause_and_resume(self):
        client = Sabnzbd(self.fake.url + '/', self.fake.apikey)
        self.assertTrue(client.pause_queue())
        self.assertTrue(self.fake.paused)
        self.assertTrue(client.resume_queue())
        self.assertFalse(self.fake.paused)
        self.assertEqual(self.fake.requests[0], ('/api', {'apikey': self.fake.apikey, 'mode': 'pause',
                                                          'output': 'json'}))

    def test_wrong_apikey(self):
        self.assertFalse(Sabnzbd(self.fake.url, 'wrong').pause_queue())
        self.assertFalse(self.fake.paused)

    def test_http_error_and_bad_json(self):
        for override in ((500, b'{"status": true}'), (200, b'<html>login</html>')):
            self.fake.override = override
            with self.subTest(override=override), self.assertLogs('utils.sabnzbd', 'ERROR') as cm:
                self.assertFalse(Sabnzbd(self.fake.url, self.fake.apikey).pause_queue())
            self.assertIn('Failed to pause the Sabnzbd queue', cm.output[0])

    def test_unreachable_server_does_not_log_apikey(self):
        with self.assertLogs('utils.sabnzbd', 'ERROR') as cm:
            self.assertFalse(Sabnzbd('http://127.0.0.1:9', 'secretKEY123456').resume_queue())
        output = formatted(cm.records)
        self.assertIn('Failed to resume', output)
        self.assertNotIn('secretKEY123456', output)

    def test_timeout_is_set(self):
        with mock.patch('utils.sabnzbd.requests.get') as get:
            get.return_value.json.return_value = {'status': True}
            Sabnzbd('http://sab:8080', 'k').pause_queue()
        self.assertEqual(get.call_args[1]['timeout'], 15)


if __name__ == '__main__':
    unittest.main()
