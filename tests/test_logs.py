import io
import logging
import sys
import unittest

from utils import logs, process


class RedactionTests(unittest.TestCase):
    def test_url_credentials(self):
        self.assertEqual(logs.redact('connect http://user:s3cret@nzbget:6789/xmlrpc failed'),
                         'connect http://***:***@nzbget:6789/xmlrpc failed')

    def test_tokens_and_apikeys(self):
        self.assertEqual(logs.redact('GET /status?X-Plex-Token=abcDEF123456789 200'), 'GET /status?X-Plex-Token=*** 200')
        self.assertEqual(logs.redact("{'apikey': 'b1bd1153190c450096d985'}"), "{'apikey': '***'}")

    def test_ordinary_text_untouched(self):
        line = 'Plex Media Server URL + Token were validated. Transferred: 2 / 2, 100%'
        self.assertEqual(logs.redact(line), line)

    def test_formatter_redacts_tracebacks(self):
        formatter = logs.RedactingFormatter('%(message)s')
        try:
            raise ValueError('bad url https://admin:hunter22@host/api')
        except ValueError:
            record = logging.LogRecord('t', logging.ERROR, __file__, 1, 'failed', None, sys.exc_info())
        output = formatter.format(record)
        self.assertIn('https://***:***@host/api', output)
        self.assertNotIn('hunter22', output)


class SetupTests(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self.saved = (list(root.handlers), root.level, logs._console_handler, logs._file_handler)
        logs._console_handler = logs._file_handler = None

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:] = self.saved[0]
        root.setLevel(self.saved[1])
        logs._console_handler, logs._file_handler = self.saved[2], self.saved[3]

    def test_console_setup_is_idempotent_and_level_mapping(self):
        logs.setup_console()
        logs.setup_console()
        console = [h for h in logging.getLogger().handlers if h is logs._console_handler]
        self.assertEqual(len(console), 1)
        logs.set_level('WARN')
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(logging.getLogger('apprise').level, logging.WARNING)


class StreamAttributionTests(unittest.TestCase):
    def test_output_is_logged_to_given_logger_as_caller(self):
        logger = logging.getLogger('tests.rclone')
        buffer = io.StringIO()
        handler = logging.StreamHandler(buffer)
        handler.setFormatter(logging.Formatter('%(name)s|%(funcName)s|%(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)

        def upload():
            process.stream([sys.executable, '-c', 'print("Transferred: 100%")'], logger=logger)

        upload()
        self.assertEqual(buffer.getvalue().strip(), 'tests.rclone|upload|Transferred: 100%')


if __name__ == '__main__':
    unittest.main()
