"""Bounded recovery of a source GET; never authorizes a media POST replay."""
import math


def connection_recovery_allowed(attempts, remaining):
    """A third GET only after two proven pre-response connection failures."""
    if (type(remaining) not in (int, float) or not math.isfinite(remaining)
            or remaining < 13 or not isinstance(attempts, list) or len(attempts) != 2):
        return False
    for attempt in attempts:
        if (not isinstance(attempt, dict) or attempt.get('success') is not False
                or attempt.get('curl_code') not in (6, 7, 28)
                or attempt.get('http_status_code') is not None
                or attempt.get('response_complete') is True
                or attempt.get('stage') != 'input_reference_download'
                or attempt.get('last_observed_phase') not in
                   ('not_observed', 'dns_completed', 'connection_completed')):
            return False
        for key in ('uploaded_bytes', 'downloaded_bytes', 'first_byte_seconds'):
            value = attempt.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value != 0:
                return False
    return True


def source_outcome(trace, attempts, retryable, complete):
    if not isinstance(trace, dict):
        return
    trace.update(success=bool(complete), attempts=len(attempts),
                 retry_safe=bool(retryable and not complete),
                 stage='input_reference_download', execution_outcome='not_started')
