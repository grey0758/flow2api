"""Bounded upload-only exit backoff; no account or quota persistence."""
import re
import time
from collections import OrderedDict


class UploadExitBackoff:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.entries = OrderedDict()
        self.observations = OrderedDict()

    def _prune_observations(self):
        now = self.clock()
        for key, entry in list(self.observations.items()):
            if now - entry['last_failure'] >= 900:
                del self.observations[key]

    def scheduler_active(self):
        self._prune_observations()
        now = self.clock()
        return {key for key, entry in self.observations.items() if entry['until'] > now}

    @staticmethod
    def _keys(account, digest):
        if type(account) is not int or not 1 <= account <= 2147483647:
            return []
        keys = [('account', account)]
        if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest):
            keys.append(('exit', digest))
        return keys

    def observe_timeout(self, account, digest=None):
        """A failed batch affects NEW upload admissions only, never replay."""
        self._prune_observations()
        now = self.clock()
        decisions = []
        for key in self._keys(account, digest):
            old = self.observations.pop(key, None)
            count = min(2, old['failures'] + 1) if old else 1
            seconds = 120 if count == 1 else 300
            # Concurrent batches cannot indefinitely extend the same open
            # cooldown. A new failure after expiry may open a new bounded one.
            opened = old['opened'] if old and old['until'] > now else now
            until = opened + seconds
            self.observations[key] = {'opened': opened, 'until': until,
                                      'last_failure': now, 'failures': count}
            decisions.append({'scope': 'account' if key[0] == 'account' else 'assigned_exit',
                              'seconds_remaining': max(0, round(until - now, 3)),
                              'failures_in_window': count})
        while len(self.observations) > 512:
            self.observations.popitem(last=False)
        return {'policy': 'upload_timeout_scheduler_v1', 'window_seconds': 900,
                'maximum_cooldown_seconds': 300, 'decisions': decisions,
                'new_reference_requests_only': True, 'authorizes_replay': False,
                'persistent_account_mutation': False}

    def observe_success(self, account, digest, batch_started):
        """An older in-flight success cannot erase a more recent failure."""
        self._prune_observations()
        cleared = []
        if not isinstance(batch_started, (int, float)):
            return cleared
        for key in self._keys(account, digest):
            entry = self.observations.get(key)
            if entry and batch_started > entry['last_failure']:
                if entry['until'] > self.clock():
                    cleared.append(key[0])
                # Retain bounded failure history through successful requests;
                # intermittent faults must still count inside the 15m window.
                entry['until'] = min(entry['until'], self.clock())
        return cleared

    def active(self):
        now = self.clock()
        for digest, (last, until, count) in list(self.entries.items()):
            if now - last >= 600:
                del self.entries[digest]
        return {digest for digest, (_, until, _) in self.entries.items() if until > now}

    def failed(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            return None
        self.active()
        now = self.clock()
        prior = self.entries.pop(digest, None)
        count = min(2, prior[2] + 1) if prior else 1
        seconds = 120 if count == 1 else 300
        self.entries[digest] = (now, now + seconds, count)
        while len(self.entries) > 255:
            self.entries.popitem(last=False)
        return {'seconds': seconds, 'scope': 'reference_upload_exit',
                'assigned_exit_sha256': digest, 'consecutive_failures': count,
                'persistent_account_mutation': False}
