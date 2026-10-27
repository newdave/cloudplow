from __future__ import annotations

import json

from sqlitedict import SqliteDict

TABLES = ('uploader_bans', 'syncer_bans', 'sa_bans', 'download_clients')


class Cache:
    """Persistent suspension state, one sqlite table per kind of ban."""

    def __init__(self, cache_file_path: str):
        self.cache_file_path = cache_file_path
        self.caches = {
            table: SqliteDict(cache_file_path, tablename=table, encode=json.dumps, decode=json.loads,
                              autocommit=True)
            for table in TABLES
        }

    def get_cache(self, cache_name: str) -> SqliteDict:
        return self.caches[cache_name]
