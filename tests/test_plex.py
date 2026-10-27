import json
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from utils.plex import Plex, PlexStream

TOKEN = 'plexTOKEN1234567890'


def movie_session(state='playing', local=True, decision='directplay', user='alice'):
    return {
        'type': 'movie', 'title': 'Blade Runner',
        'User': {'id': '1', 'title': user},
        'Player': {'product': 'Plex Web', 'state': state, 'local': local, 'address': '10.0.0.5'},
        'Session': {'id': 'sess-movie', 'bandwidth': 12000, 'location': 'lan' if local else 'wan'},
        'Media': [{'id': '1', 'Part': [{'id': '1', 'decision': decision, 'file': '/data/movies/br.mkv'}]}],
    }


def episode_session(state='paused', local=False):
    return {
        'type': 'episode', 'title': 'Pilot', 'grandparentTitle': 'Severance', 'parentIndex': 1, 'index': 1,
        'User': {'title': 'bob'},
        'Player': {'product': 'Plex for Android (TV)', 'state': state, 'local': local},
        'Session': {'id': 'sess-ep'},
        'Media': [{'Part': [{'decision': 'transcode'}]}],
        'TranscodeSession': {'videoDecision': 'transcode', 'audioDecision': 'copy', 'throttled': False},
    }


class FakePlex:
    """Serves /status/sessions like Plex Media Server does when asked for JSON."""

    def __init__(self, token=TOKEN):
        self.token = token
        self.sessions = []
        self.status = 200
        self.content_type = 'application/json'
        self.body = None
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.requests.append((self.path, dict(self.headers)))
                status, ctype, body = fake.respond(self.path, self.headers)
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/'
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, path, headers):
        if headers.get('X-Plex-Token') != self.token:
            return 401, 'text/html', b'<html><head><title>Unauthorized</title></head></html>'
        if path != '/status/sessions':
            return 404, 'text/html', b'Not Found'
        if self.body is not None:
            return self.status, self.content_type, self.body
        container = {'size': len(self.sessions)}
        if self.sessions:
            container['Metadata'] = self.sessions
        return self.status, self.content_type, json.dumps({'MediaContainer': container}).encode()


class PlexServerTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePlex()
        self.addCleanup(self.fake.close)
        self.plex = Plex(self.fake.url, TOKEN)

    def test_request_headers(self):
        self.assertTrue(self.plex.validate())
        path, headers = self.fake.requests[0]
        self.assertEqual(path, '/status/sessions')
        self.assertEqual(headers['X-Plex-Token'], TOKEN)
        self.assertEqual(headers['Accept'], 'application/json')
        self.assertEqual(headers['X-Plex-Product'], 'cloudplow')
        self.assertTrue(headers['X-Plex-Client-Identifier'].startswith('0x'))

    def test_wrong_token_fails_validation(self):
        with self.assertLogs('utils.plex', 'ERROR') as cm:
            self.assertFalse(Plex(self.fake.url, 'wrong').validate())
            self.assertIsNone(Plex(self.fake.url, 'wrong').get_streams())
        self.assertIn('status_code=401', cm.output[0])

    def test_idle_server_returns_empty_list(self):
        self.assertEqual(self.plex.get_streams(), [])

    def test_streams_are_parsed(self):
        self.fake.sessions = [movie_session(), episode_session()]
        movie, episode = self.plex.get_streams()

        self.assertEqual((movie.user, movie.player, movie.title), ('alice', 'Plex Web', 'Blade Runner'))
        self.assertEqual((movie.state, movie.local, movie.session_id), ('playing', True, 'sess-movie'))
        self.assertEqual(movie.type, 'directplay')
        self.assertEqual((movie.video_decision, movie.audio_decision), ('directplay', 'directplay'))
        self.assertTrue(movie.active)

        self.assertEqual(episode.title, 'Severance 1x1')
        self.assertFalse(episode.local)
        self.assertFalse(episode.active)
        self.assertEqual(episode.type, 'transcode')
        self.assertEqual((episode.video_decision, episode.audio_decision), ('transcode', 'copy'))

    def test_legacy_video_key(self):
        self.fake.body = json.dumps({'MediaContainer': {'size': 1, 'Video': [movie_session(state='buffering')]}}).encode()
        streams = self.plex.get_streams()
        self.assertEqual(len(streams), 1)
        self.assertTrue(streams[0].active)

    def test_xml_response_is_rejected(self):
        # Plex answers XML when the Accept header is ignored (e.g. by a proxy); only JSON is parsed
        self.fake.content_type = 'text/xml;charset=utf-8'
        self.fake.body = (b'<?xml version="1.0" encoding="UTF-8"?>\n<MediaContainer size="1">'
                          b'<Video title="Blade Runner" type="movie"><Player state="playing" local="1"/></Video>'
                          b'</MediaContainer>')
        with self.assertLogs('utils.plex', 'ERROR'):
            self.assertIsNone(self.plex.get_streams())
        with self.assertLogs('utils.plex', 'ERROR'):
            self.assertFalse(self.plex.validate())

    def test_json_without_media_container(self):
        self.fake.body = b'{"errors": [{"code": 1001}]}'
        with self.assertLogs('utils.plex', 'ERROR') as cm:
            self.assertIsNone(self.plex.get_streams())
        self.assertIn('Failed to retrieve streams', cm.output[0])
        self.assertTrue(self.plex.validate())   # the server did answer

    def test_server_error(self):
        self.fake.status = 500
        with self.assertLogs('utils.plex', 'ERROR') as cm:
            self.assertIsNone(self.plex.get_streams())
        self.assertIn('status_code=500', cm.output[0])

    def test_token_never_logged(self):
        self.fake.sessions = [movie_session()]
        with self.assertLogs('utils.plex', 'DEBUG') as cm:
            self.plex.get_streams()
            self.fake.status = 500
            self.plex.get_streams()
            Plex('http://127.0.0.1:9/', TOKEN).get_streams()
        records = '\n'.join(cm.output)
        formatter = logging.Formatter('%(message)s')
        records += '\n'.join(formatter.format(r) + (formatter.formatException(r.exc_info) if r.exc_info else '')
                             for r in cm.records)
        self.assertIn('status_code=500', records)
        self.assertNotIn(TOKEN, records)

    def test_unreachable_server(self):
        with self.assertLogs('utils.plex', 'ERROR') as cm:
            plex = Plex('http://127.0.0.1:9/', TOKEN)
            self.assertFalse(plex.validate())
            self.assertIsNone(plex.get_streams())
        self.assertIn('Exception retrieving sessions', cm.output[0])


class PlexStreamTests(unittest.TestCase):
    def test_minimal_session_defaults_to_unknown(self):
        stream = PlexStream({})
        self.assertEqual((stream.user, stream.player, stream.state, stream.session_id, stream.title, stream.type),
                         ('Unknown',) * 6)
        self.assertIsNone(stream.local)
        self.assertFalse(stream.active)

    def test_decision_is_first_part_that_has_one(self):
        medias = [{'Part': [{'id': 1}]}, {'Part': [{'decision': 'copy'}, {'decision': 'transcode'}]}]
        self.assertEqual(PlexStream.get_decision(medias), 'copy')
        self.assertEqual(PlexStream.get_decision([{}]), 'Unknown')

    def test_transcode_without_session_details(self):
        session = movie_session(decision='transcode')
        stream = PlexStream(session)
        self.assertEqual((stream.video_decision, stream.audio_decision), ('Unknown', 'Unknown'))
        self.assertIn('type: transcode ().', str(stream))

    def test_str_describes_stream(self):
        self.assertEqual(str(PlexStream(movie_session(local=False))),
                         'alice is playing Blade Runner using Plex Web. Stream state: playing, local: False, '
                         'type: directplay.')
        episode = PlexStream(episode_session())
        self.assertIn('type: transcode (video).', str(episode))
        both = episode_session()
        both['TranscodeSession']['audioDecision'] = 'transcode'
        self.assertIn('type: transcode (video & audio).', repr(PlexStream(both)))

    def test_title_needs_type(self):
        session = movie_session()
        del session['type']
        self.assertEqual(PlexStream(session).title, 'Unknown')


if __name__ == '__main__':
    unittest.main()
