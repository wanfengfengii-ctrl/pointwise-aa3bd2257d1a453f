import json
from pathlib import Path
from tempfile import TemporaryDirectory

from archive_db import ReplyLost
from corrections.pending import FieldsBusy
from corrections.recover import CancelledCorrection, resume
from corrections.request import amend, return_retest
from delivery.current import current, issue_retest, read_retest
from delivery.reconcile import reconcile
from delivery.snapshot import frozen
from intake.manifest import ManifestEntry
from intake.register import ingest, initialise_archive
from intake.validate import InputRow
from layout.registry import define
from layout.spec import FieldSpec, LayoutSpec
from layout.values import MISSING


LAYOUT = LayoutSpec('calibration-8', 8, 2, (
    FieldSpec('x', 0, 0, 3, 7), FieldSpec('y', 1, 0, 3, 7), FieldSpec('z', 0, 3, 3, 7),
))
ROWS = (InputRow('R-20', 4, 2, (17, 3)), InputRow('R-02', 5, 1, (184, 128)))
NIGHT_ROWS = (InputRow('R-20', 4, 2, (23, 7)), ROWS[1])
MANIFEST = (ManifestEntry('R-20', 2, 4, 2), ManifestEntry('R-02', 2, 5, 1))
A_CHANGES = (('R-20', 'x', 4), ('R-20', 'y', 5))
B_CHANGES = (('R-20', 'z', 6),)


def lose_at(event_name):
    def lose(event, detail):
        if event == event_name:
            raise ReplyLost(event, detail)
    return lose


def reading_group(observations):
    return tuple(None if item.value is MISSING else item.value
                 for item in observations if item.row_id == 'R-20')


def source_group(observations):
    return tuple(item.provenance.request_id for item in observations if item.row_id == 'R-20')


def _scenario(path, action):
    volume = f'day-{action}'
    alpha = f'alpha-{action}'
    beta = f'beta-{action}'
    accepted = ingest(path, volume, LAYOUT.layout_id, ROWS, MANIFEST)
    original = frozen(path, accepted.report_id)
    assert reading_group(original.observations) == (1, 3, 2)
    assert tuple(item.value for item in original.observations[-3:]) == (0, 0, MISSING)
    try:
        amend(path, volume, alpha, A_CHANGES, hook=lose_at('after_word_commit'))
    except ReplyLost as loss:
        assert (loss.detail.before, loss.detail.after) == (17, 20)
    else:
        raise AssertionError('The first word reply was delivered')
    signed_beta = amend(path, volume, beta, B_CHANGES)
    paused = current(path, volume)
    pending = (reading_group(paused), source_group(paused))
    expected_pending = ((1, 3, 6), (None, None, beta))
    assert pending == expected_pending, ('PACKED_CORRECTION_MIXED', pending, expected_pending)
    initialise_archive(path)
    result = resume(path, alpha, action=action)
    final = current(path, volume)
    expected = (4, 5, 6) if action == 'continue' else (1, 3, 6)
    expected_sources = (alpha, alpha, beta) if action == 'continue' else (None, None, beta)
    assert (reading_group(final), source_group(final)) == (expected, expected_sources), (
        'PACKED_CORRECTION_MIXED', reading_group(final), source_group(final))
    assert resume(path, alpha) == result
    assert resume(path, beta) == signed_beta
    ledger = reconcile(path, volume)
    final_words = tuple(word.actual_word for word in ledger.words[:2])
    expected_words = (52, 5) if action == 'continue' else (49, 3)
    assert ledger.settled and final_words == expected_words, ('PACKED_CORRECTION_MIXED', final_words, expected_words)
    assert frozen(path, accepted.report_id) == original
    assert frozen(path, signed_beta.report_id).receipt == signed_beta
    record = {
        'case_id': 'v5.interleaved_disjoint' if action == 'continue' else 'v6.masked_cancel',
        'input': {'original_words': ROWS[0].words, 'alpha': A_CHANGES, 'beta': B_CHANGES, 'action': action},
        'expected': {'values': expected_pending[0], 'sources': expected_pending[1]},
        'actual': {'values': pending[0], 'sources': pending[1]},
        'downstream_expected': {'values': expected, 'sources': expected_sources, 'words': expected_words},
        'downstream_actual': {'values': reading_group(final), 'sources': source_group(final), 'words': final_words},
        'code_paths': ['corrections/recover.py', 'words/write.py', 'delivery/current.py', 'delivery/reconcile.py'],
        'oracle': '首字手算为 x + 8*z，次字低三位为 y。出处由甲乙两单的实际发布切点核对。',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(record, ensure_ascii=False, sort_keys=True))


def _night_moves(path, volume, action):
    """The shared night-shift opening: tickets, re-missing, rejection and the paused return."""
    ingest(path, volume, LAYOUT.layout_id, NIGHT_ROWS, MANIFEST)
    opened_p = issue_retest(path, volume, f'P-{action}', ('y', 'x'))
    assert tuple(item['field_name'] for item in opened_p['items']) == ('x', 'y')
    assert all(item['opened'].kind == 'original' for item in opened_p['items'])
    amend(path, volume, f'repeat-missing-{action}', (('R-20', 'x', MISSING),))
    opened_p2 = issue_retest(path, volume, f'P2-{action}', ('x',))
    assert opened_p2['items'][0]['opened'].kind == 'original'
    amend(path, volume, f'ordinary-fill-{action}', (('R-20', 'x', 2),))
    amend(path, volume, f'missing-again-{action}', (('R-20', 'x', MISSING),))
    opened_q = issue_retest(path, volume, f'Q-{action}', ('x', 'y'))
    issue_retest(path, volume, f'Q2-{action}', ('x',))
    assert opened_q['items'][0]['opened'].request_id == f'missing-again-{action}'
    assert opened_q['items'][1]['opened'].kind == 'original'
    rejected = return_retest(path, f'P-{action}', f'R-P-{action}', ((1, 5), (2, 4)))
    assert rejected['status'] == 'rejected'
    assert tuple(item['item_id'] for item in rejected['blocked']) == (1,)
    assert rejected['blocked'][0]['closed_by'].request_id == f'ordinary-fill-{action}'
    try:
        return_retest(path, f'Q-{action}', f'R-Q-{action}', ((1, 0), (2, 4)), hook=lose_at('after_word_commit'))
    except ReplyLost as loss:
        assert (loss.detail.before, loss.detail.after) == (23, 16)
    else:
        raise AssertionError('The first word reply was delivered')
    amend(path, volume, f'shared-word-{action}', (('R-20', 'z', 6),))
    try:
        amend(path, volume, f'same-field-{action}', (('R-20', 'x', 5),))
    except FieldsBusy as busy:
        assert busy.holder == f'R-Q-{action}'
    else:
        raise AssertionError('expected busy field')
    paused = {
        'blocked': tuple(item['item_id'] for item in rejected['blocked']),
        'q_items': tuple(item['status'] for item in read_retest(path, f'Q-{action}')['items']),
        'word0': reconcile(path, volume).words[0].actual_word,
        'busy_holder': f'R-Q-{action}',
    }
    expected_paused = {'blocked': (1,), 'q_items': ('pending', 'pending'),
                       'word0': 48, 'busy_holder': f'R-Q-{action}'}
    assert paused == expected_paused, ('RETEST_PAUSED', paused, expected_paused)
    return rejected, paused, expected_paused


def _night_scenario(path, action):
    volume = f'night-{action}'
    rejected, paused, expected_paused = _night_moves(path, volume, action)
    initialise_archive(path)
    if action == 'continue':
        receipt = resume(path, f'R-Q-{action}')
        closed_view = read_retest(path, f'Q-{action}')
        assert all(item['closed_by'].request_id == f'R-Q-{action}' for item in closed_view['items'])
        filled_words = tuple(word.actual_word for word in reconcile(path, volume).words[:2])
        assert filled_words == (48, 4), ('RETEST_FILLED_WORDS', filled_words)
        amend(path, volume, f'later-missing-{action}', (('R-20', 'x', MISSING),))
        assert read_retest(path, f'Q-{action}') == closed_view
        assert return_retest(path, f'Q-{action}', f'R-Q-{action}', ((2, 4), (1, 0))) == receipt
        assert return_retest(path, f'P-{action}', f'R-P-{action}', ((1, 5), (2, 4))) == rejected
        assert frozen(path, receipt.report_id).receipt == receipt
        expected = {
            'q_items': ('filled', 'filled'), 'q2_x': 'superseded', 'p_items': ('corrected', 'superseded'),
            'words': (55, 4), 'values': (None, 4, 6),
            'sources': (f'later-missing-{action}', f'R-Q-{action}', f'shared-word-{action}'),
        }
    else:
        cancelled = resume(path, f'R-Q-{action}', action='cancel')
        assert cancelled == CancelledCorrection(f'R-Q-{action}', volume)
        assert return_retest(path, f'Q-{action}', f'R-Q-{action}', ((1, 0), (2, 4))) == cancelled
        expected = {
            'q_items': ('pending', 'pending'), 'q2_x': 'pending', 'p_items': ('corrected', 'pending'),
            'words': (55, 7), 'values': (None, None, 6),
            'sources': (f'missing-again-{action}', None, f'shared-word-{action}'),
        }
    ledger = reconcile(path, volume)
    now = current(path, volume)
    actual = {
        'q_items': tuple(item['status'] for item in read_retest(path, f'Q-{action}')['items']),
        'q2_x': read_retest(path, f'Q2-{action}')['items'][0]['status'],
        'p_items': tuple(item['status'] for item in read_retest(path, f'P-{action}')['items']),
        'words': tuple(word.actual_word for word in ledger.words[:2]),
        'values': reading_group(now),
        'sources': source_group(now),
    }
    assert ledger.settled and actual == expected, ('RETEST_FINAL', actual, expected)
    record = {
        'case_id': 'v7.retest_filled' if action == 'continue' else 'v8.retest_cancelled',
        'input': {'original_words': NIGHT_ROWS[0].words,
                  'tickets': {'P': ['y', 'x'], 'P2': ['x'], 'Q': ['x', 'y'], 'Q2': ['x']},
                  'returns': {'R-P': [[1, 5], [2, 4]], 'R-Q': [[1, 0], [2, 4]]},
                  'action': action},
        'expected': expected_paused,
        'actual': paused,
        'downstream_expected': expected,
        'downstream_actual': actual,
        'code_paths': ['delivery/retest.py', 'corrections/request.py', 'corrections/retest.py',
                       'corrections/recover.py'],
        'oracle': '首字手算为 x + 8*z，次字低三位为 y。欠测起点与结案出处按发布序核对，拒收单只列当时已结案项。',
    }
    print('POINTWISE_SCENARIO ' + json.dumps(record, ensure_ascii=False, sort_keys=True))


def main():
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'day.sqlite3'
        initialise_archive(path)
        define(path, LAYOUT)
        _scenario(path, 'continue')
        _scenario(path, 'cancel')
        _night_scenario(path, 'continue')
        _night_scenario(path, 'cancel')


if __name__ == '__main__':
    main()
