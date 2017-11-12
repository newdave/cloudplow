import logging

from . import process

try:
    from shlex import quote as cmd_quote
except ImportError:
    from pipes import quote as cmd_quote

log = logging.getLogger('rclone')


class Rclone:
    def __init__(self, name, config, dry_run=False):
        self.name = name
        self.config = config
        self.dry_run = dry_run

    def delete_file(self, path):
        try:
            log.debug("Deleting file '%s' from remote %s", path, self.name)
            # build cmd
            cmd = "rclone delete %s" % cmd_quote(path)
            if self.dry_run:
                cmd += ' --dry-run'

            # exec
            log.debug("Using: %s", cmd)
            resp = process.execute(cmd)
            if 'Failed to delete' in resp:
                return False

            return True
        except:
            log.exception("Exception deleting file '%s' from remote %s: ", path, self.name)
        return False

    def delete_folder(self, path):
        try:
            log.debug("Deleting folder '%s' from remote %s", path, self.name)
            # build cmd
            cmd = "rclone rmdir %s" % cmd_quote(path)
            if self.dry_run:
                cmd += ' --dry-run'

            # exec
            log.debug("Using: %s", cmd)
            resp = process.execute(cmd)
            if 'Failed to rmdir' in resp:
                return False

            return True
        except:
            log.exception("Exception deleting folder '%s' from remote %s: ", path, self.name)
        return False

    def upload(self, callback):
        try:
            log.debug("Uploading '%s' to '%s'", self.config['upload_folder'], self.config['upload_remote'])
            # build cmd
            cmd = "rclone move %s %s" % (
                cmd_quote(self.config['upload_folder']), cmd_quote(self.config['upload_remote']))

            extras = self.__extras2string()
            if len(extras) > 2:
                cmd += ' %s' % extras
            excludes = self.__excludes2string()
            if len(excludes) > 2:
                cmd += ' %s' % excludes
            if self.dry_run:
                cmd += ' --dry-run'

            # exec
            log.debug("Using: %s", cmd)
            process.execute(cmd, callback)
            return True
        except:
            log.exception("Exception occurred while uploading '%s' to remote: %s", self.config['upload_folder'],
                          self.name)
        return False

    # internals
    def __extras2string(self):
        return ' '.join(
            "%s=%s" % (key, cmd_quote(value) if isinstance(value, str) else value) for (key, value) in
            self.config['rclone_extras'].items()).replace('=None', '').strip()

    def __excludes2string(self):
        return ' '.join(
            "--exclude=%s" % (cmd_quote(value) if isinstance(value, str) else value) for value in
            self.config['rclone_excludes']).replace('=None', '').strip()
