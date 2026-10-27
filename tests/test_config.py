import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from utils import config

ENV_NAMES = ('CLOUDPLOW_CONFIG', 'CLOUDPLOW_LOGFILE', 'CLOUDPLOW_CACHEFILE', 'CLOUDPLOW_LOGLEVEL')


def existing_user_config():
    """A config as an existing (e.g. Saltbox-managed) install has it: customised, partly outdated."""
    return {
        'core': {'dry_run': True, 'rclone_binary_path': '/usr/local/bin/rclone',
                 'rclone_config_path': '/home/seed/.config/rclone/rclone.conf'},
        'hidden': {},
        'notifications': {'apprise': {'service': 'apprise', 'url': 'discord://abc/def'}},
        'plex': {
            'enabled': True,
            'url': 'http://plex:32400',
            'token': 'user-plex-token',
            'poll_interval': 30,
            'max_streams_before_throttle': 2,
            'ignore_local_streams': False,
            'notifications': True,
            'rclone': {'url': 'http://localhost:7949',
                       'throttle_speeds': {'1': '80M', '2': '60M'}},
        },
        'nzbget': {'enabled': True, 'url': 'http://u:p@nzbget:6789'},
        'sabnzbd': {'enabled': False, 'url': 'http://sab:8080', 'apikey': 'k'},
        # 'qbittorrent' is missing: an install from before the section existed
        'remotes': {'google': {'upload_folder': '/mnt/local/Media', 'upload_remote': 'google:/Media',
                               'rclone_excludes': ['**partial~'], 'remove_empty_dir_depth': 2}},
        'uploader': {'google': {'check_interval': 30, 'post_script': '/opt/scripts/post.sh',
                                'rate_script': '/opt/scripts/rate.sh', 'verified_move': True,
                                'max_size_gb': 400, 'size_excludes': ['downloads/*']}},
        'syncer': {},
        'some_future_section': {'kept': True},
    }


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cfg_path = os.path.join(self.tmp, 'config.json')
        self.log_path = os.path.join(self.tmp, 'cloudplow.log')
        self.cache_path = os.path.join(self.tmp, 'cache.db')
        # no CLOUDPLOW_* or config-section variables from the developer's shell
        env = {k: v for k, v in os.environ.items()
               if k not in ENV_NAMES and k not in config.Config.base_config}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def argv(self, *extra, cmd='update_config'):
        return ['cloudplow.py', cmd, '--config', self.cfg_path, '--logfile', self.log_path,
                '--cachefile', self.cache_path] + list(extra)

    def make(self, argv=None):
        with mock.patch.object(sys, 'argv', argv or self.argv()), self.assertLogs('utils.config', 'DEBUG'):
            return config.Config()

    def write(self, data):
        with open(self.cfg_path, 'w') as fp:
            json.dump(data, fp)

    def read(self):
        with open(self.cfg_path) as fp:
            return json.load(fp)

    def load(self, conf):
        """Run conf.load(); returns (exited, logs). save() always exits, as `update_config` relies on."""
        with self.assertLogs('utils.config', 'DEBUG') as cm:
            try:
                conf.load()
            except SystemExit as exc:
                self.assertEqual(exc.code, 0)
                return True, cm.output
        return False, cm.output


class ArgsAndSettingsTests(ConfigTestCase):
    def test_cli_paths_are_used(self):
        conf = self.make(self.argv('--loglevel', 'DEBUG', cmd='upload'))
        self.assertEqual(conf.args['cmd'], 'upload')
        self.assertEqual(conf.settings, {'config': self.cfg_path, 'logfile': self.log_path,
                                         'cachefile': self.cache_path, 'loglevel': 'DEBUG'})

    def test_env_used_when_no_cli_argument(self):
        os.environ.update({'CLOUDPLOW_CONFIG': '/env/config.json', 'CLOUDPLOW_LOGFILE': '/env/log',
                           'CLOUDPLOW_CACHEFILE': '/env/cache.db', 'CLOUDPLOW_LOGLEVEL': 'WARNING'})
        conf = self.make(['cloudplow.py', 'run'])
        self.assertEqual(conf.settings, {'config': '/env/config.json', 'logfile': '/env/log',
                                         'cachefile': '/env/cache.db', 'loglevel': 'WARNING'})

    def test_cli_beats_env(self):
        os.environ['CLOUDPLOW_CONFIG'] = '/env/config.json'
        os.environ['CLOUDPLOW_LOGLEVEL'] = 'ERROR'
        conf = self.make(self.argv('--loglevel', 'INFO'))
        self.assertEqual(conf.settings['config'], self.cfg_path)
        self.assertEqual(conf.settings['loglevel'], 'INFO')

    def test_defaults_when_nothing_given(self):
        conf = self.make(['cloudplow.py', 'sync'])
        base = config.Config.base_settings
        self.assertEqual(conf.settings['loglevel'], 'INFO')
        for name in ('config', 'logfile', 'cachefile'):
            self.assertEqual(conf.settings[name], base[name]['default'])
        self.assertEqual(os.path.basename(conf.settings['config']), 'config.json')

    def test_option_without_value_falls_back_to_env(self):
        os.environ['CLOUDPLOW_CONFIG'] = '/env/config.json'
        conf = self.make(['cloudplow.py', 'run', '--config'])
        self.assertEqual(conf.settings['config'], '/env/config.json')

    def test_no_arguments_prints_help_and_exits(self):
        with mock.patch.object(sys, 'argv', ['cloudplow.py']), mock.patch('sys.stdout') as stdout:
            with self.assertRaises(SystemExit) as cm:
                config.Config()
        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(stdout.write.called)

    def test_invalid_command_and_level_are_rejected(self):
        for argv in (['cloudplow.py', 'explode'], ['cloudplow.py', 'run', '--loglevel', 'TRACE']):
            with self.subTest(argv=argv), mock.patch.object(sys, 'argv', argv), mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit) as cm:
                    config.Config()
                self.assertEqual(cm.exception.code, 2)


class LoadAndUpgradeTests(ConfigTestCase):
    def test_missing_config_file_creates_default_and_exits(self):
        conf = self.make()
        exited, output = self.load(conf)
        self.assertTrue(exited)
        self.assertTrue(any('No config file found' in line for line in output))
        created = self.read()
        self.assertEqual(created, json.loads(json.dumps(conf.default_config)))
        for section in config.Config.base_config:
            self.assertIn(section, created)
        self.assertIn('google', created['remotes'])
        self.assertIn('post_script', created['uploader']['google'])

        # the next start loads it without rewriting it
        exited, _ = self.load(self.make())
        self.assertFalse(exited)

    def test_upgrade_adds_missing_defaults_and_keeps_user_values(self):
        user = existing_user_config()
        self.write(user)
        exited, output = self.load(self.make())
        self.assertTrue(exited)
        self.assertTrue(any('Added qbittorrent config option' in line for line in output))

        upgraded = self.read()
        self.assertEqual(upgraded['qbittorrent'], config.Config.base_config['qbittorrent'])
        # nothing the user had is removed or changed, unknown keys included
        for section, value in user.items():
            if section == 'plex':
                continue
            self.assertEqual(upgraded[section], value, section)
        plex = upgraded['plex']
        for key in ('enabled', 'url', 'token', 'poll_interval', 'max_streams_before_throttle',
                    'ignore_local_streams', 'notifications'):
            self.assertEqual(plex[key], user['plex'][key], key)
        self.assertEqual(plex['rclone']['throttle_speeds']['1'], '80M')
        self.assertEqual(plex['rclone']['throttle_speeds']['2'], '60M')
        # missing tiers are filled in from the defaults
        self.assertEqual(plex['rclone']['throttle_speeds']['5'], '10M')

    def test_upgrade_keeps_post_script_and_other_unknown_uploader_keys(self):
        user = existing_user_config()
        self.write(user)
        self.load(self.make())
        uploader = self.read()['uploader']['google']
        for key in ('post_script', 'rate_script', 'verified_move', 'max_size_gb', 'size_excludes'):
            self.assertEqual(uploader[key], user['uploader']['google'][key], key)

    def test_nested_missing_key_is_added(self):
        user = existing_user_config()
        user['qbittorrent'] = {'enabled': True, 'url': 'http://qb:8080'}
        del user['plex']['rclone']['url']
        self.write(user)
        exited, output = self.load(self.make())
        self.assertTrue(exited)
        upgraded = self.read()
        self.assertEqual(upgraded['qbittorrent'], {'enabled': True, 'url': 'http://qb:8080',
                                                   'username': '', 'password': ''})
        self.assertEqual(upgraded['plex']['rclone']['url'], 'http://localhost:7949')
        self.assertTrue(any('Added url to config option rclone' in line for line in output))

    def test_up_to_date_config_is_not_rewritten(self):
        user = existing_user_config()
        self.write(user)
        self.load(self.make())   # first upgrade
        with open(self.cfg_path) as fp:
            before = fp.read()
        exited, output = self.load(self.make())
        self.assertFalse(exited)
        with open(self.cfg_path) as fp:
            self.assertEqual(fp.read(), before)
        self.assertTrue(any('not upgraded' in line for line in output))

    def test_update_config_is_idempotent(self):
        """Saltbox runs `update_config` on every install: repeated runs must converge."""
        self.write(existing_user_config())
        self.load(self.make())
        first = self.read()
        for _ in range(2):
            exited, _ = self.load(self.make())
            self.assertFalse(exited)
        self.assertEqual(self.read(), first)

    def test_loaded_config_exposed_on_configs(self):
        user = existing_user_config()
        self.write(user)
        self.load(self.make())
        conf = self.make()
        self.load(conf)
        self.assertEqual(conf.configs['plex']['token'], 'user-plex-token')
        self.assertEqual(conf.configs['uploader']['google']['post_script'], '/opt/scripts/post.sh')

    def test_saved_file_is_sorted_indented_and_newline_terminated(self):
        self.write(existing_user_config())
        self.load(self.make())
        with open(self.cfg_path) as fp:
            text = fp.read()
        self.assertTrue(text.endswith('}\n'))
        self.assertIn('\n  "core": {', text)
        keys = list(json.loads(text))
        self.assertEqual(keys, sorted(keys))

    def test_invalid_json_is_not_overwritten(self):
        with open(self.cfg_path, 'w') as fp:
            fp.write('{"core": ')
        with self.assertRaises(ValueError):
            self.make().load()
        with open(self.cfg_path) as fp:
            self.assertEqual(fp.read(), '{"core": ')


class EnvOverrideTests(ConfigTestCase):
    def complete_config(self):
        cfg = existing_user_config()
        cfg['qbittorrent'] = dict(config.Config.base_config['qbittorrent'])
        cfg['plex']['rclone']['throttle_speeds'] = dict(config.Config.base_config['plex']['rclone']['throttle_speeds'])
        return cfg

    def test_env_section_replaces_file_section_in_memory(self):
        self.write(self.complete_config())
        env_sabnzbd = {'enabled': True, 'url': 'http://env-sab:8080', 'apikey': 'fromenv'}
        os.environ['sabnzbd'] = json.dumps(env_sabnzbd)
        conf = self.make()
        exited, output = self.load(conf)
        self.assertFalse(exited)
        self.assertEqual(conf.configs['sabnzbd'], env_sabnzbd)
        self.assertTrue(any('Using ENV setting for config section: sabnzbd' in line for line in output))
        # the file keeps its own value
        self.assertEqual(self.read()['sabnzbd']['url'], 'http://sab:8080')

    def test_partial_env_section_gets_defaults(self):
        conf = self.make()
        os.environ['qbittorrent'] = json.dumps({'enabled': True})
        merged, upgraded = conf.upgrade_settings({'qbittorrent': {}})
        self.assertTrue(upgraded)
        self.assertEqual(merged['qbittorrent'], {'enabled': True, 'url': 'http://qbittorrent:8080',
                                                 'username': '', 'password': ''})

    def test_only_known_sections_are_read_from_env(self):
        conf = self.make()
        os.environ['remotes_extra'] = '{"x": 1}'
        os.environ['PATH_LIKE'] = 'not json'
        merged, _ = conf.upgrade_settings({})
        self.assertNotIn('remotes_extra', merged)

    def test_invalid_env_json_raises(self):
        conf = self.make()
        os.environ['plex'] = '{not json'
        with self.assertRaises(ValueError):
            conf.upgrade_settings({})

    def test_env_override_is_not_persisted_by_an_upgrade(self):
        user = existing_user_config()   # missing qbittorrent: an upgrade will be saved
        self.write(user)
        os.environ['sabnzbd'] = json.dumps({'enabled': True, 'url': 'http://env-sab', 'apikey': 'env-secret'})
        exited, _ = self.load(self.make())
        self.assertTrue(exited)
        self.assertEqual(self.read()['sabnzbd'], user['sabnzbd'])


class InnerUpgradeTests(ConfigTestCase):
    def upgrade(self, defaults, current, **kwargs):
        # the method is name-mangled; overwrite/lists are not reachable via upgrade_settings
        return self.make()._Config__inner_upgrade(defaults, current, **kwargs)

    def test_list_defaults_are_appended_without_dropping_user_entries(self):
        merged, upgraded = self.upgrade({'excludes': ['a', 'b']}, {'excludes': ['mine', 'a']})
        self.assertTrue(upgraded)
        self.assertEqual(merged['excludes'], ['mine', 'a', 'b'])

    def test_nothing_to_add(self):
        merged, upgraded = self.upgrade({'a': 1, 'b': {'c': 2}}, {'a': 5, 'b': {'c': 9}, 'd': 0})
        self.assertFalse(upgraded)
        self.assertEqual(merged, {'a': 5, 'b': {'c': 9}, 'd': 0})

    def test_input_config_is_not_mutated(self):
        current = {'plex': {'url': 'x'}}
        snapshot = json.loads(json.dumps(current))
        self.upgrade({'plex': {'url': 'd', 'token': ''}}, current)
        self.assertEqual(current, snapshot)

    def test_overwrite_only_replaces_differing_value(self):
        merged, _ = self.upgrade({'a': 1, 'b': 2}, {'a': 5, 'b': 2, 'user_key': 'keep'}, overwrite=True)
        self.assertEqual(merged, {'a': 1, 'b': 2, 'user_key': 'keep'})


if __name__ == '__main__':
    unittest.main()
