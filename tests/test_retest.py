from archive_db import ConflictError, NotFound, ReplyLost
from corrections.pending import FieldsBusy
from corrections.recover import CancelledCorrection, resume
from corrections.request import amend, return_retest
from delivery.current import current, issue_retest, read_retest
from delivery.reconcile import reconcile
from delivery.snapshot import frozen
from demo import LAYOUT, MANIFEST, NIGHT_ROWS, lose_at
from intake.register import ingest, initialise_archive
from layout.values import MISSING
from tests.support import ArchiveTestCase


class RetestTests(ArchiveTestCase):
    def night(self, volume='night'):
        return ingest(self.path, volume, LAYOUT.layout_id, NIGHT_ROWS, MANIFEST)

    def open_episodes(self, volume='night'):
        """The shared opening moves: P, repeat-missing, P2, ordinary-fill, missing-again, Q, Q2."""
        self.night(volume)
        issue_retest(self.path, volume, 'P', ('y', 'x'))
        amend(self.path, volume, 'repeat-missing', (('R-20', 'x', MISSING),))
        issue_retest(self.path, volume, 'P2', ('x',))
        amend(self.path, volume, 'ordinary-fill', (('R-20', 'x', 2),))
        amend(self.path, volume, 'missing-again', (('R-20', 'x', MISSING),))
        issue_retest(self.path, volume, 'Q', ('x', 'y'))
        issue_retest(self.path, volume, 'Q2', ('x',))

    def pause_q_return(self):
        with self.assertRaises(ReplyLost) as lost:
            return_retest(self.path, 'Q', 'R-Q', ((1, 0), (2, 4)), hook=lose_at('after_word_commit'))
        self.assertEqual((lost.exception.detail.before, lost.exception.detail.after), (23, 16))

    def test_issue_orders_items_by_row_and_layout(self):
        self.night()
        ticket = issue_retest(self.path, 'night', 'P', ('y', 'x'))
        self.assertEqual((ticket['ticket_id'], ticket['volume_id']), ('P', 'night'))
        self.assertEqual(tuple((item['item_id'], item['row_id'], item['field_name']) for item in ticket['items']),
                         ((1, 'R-20', 'x'), (2, 'R-20', 'y')))
        self.assertTrue(all(item['opened'].kind == 'original' for item in ticket['items']))
        full = issue_retest(self.path, 'night', 'ALL')
        self.assertEqual(tuple((item['row_id'], item['field_name']) for item in full['items']),
                         (('R-20', 'x'), ('R-20', 'y'), ('R-02', 'z')))

    def test_empty_ticket_allowed(self):
        self.accept()
        ticket = issue_retest(self.path, 'day', 'E', ('x', 'y'))
        self.assertEqual(ticket['items'], ())
        self.assertEqual(read_retest(self.path, 'E')['items'], ())
        self.assertEqual(issue_retest(self.path, 'day', 'E', ('y', 'x')), ticket)

    def test_issue_reclaim_and_conflict(self):
        self.night()
        first = issue_retest(self.path, 'night', 'P', ('y', 'x'))
        self.assertEqual(issue_retest(self.path, 'night', 'P', ('x', 'y')), first)
        full = issue_retest(self.path, 'night', 'ALL')
        self.assertEqual(issue_retest(self.path, 'night', 'ALL', ('z', 'x', 'y')), full)
        with self.assertRaises(ConflictError):
            issue_retest(self.path, 'night', 'P', ('x',))
        with self.assertRaises(ConflictError):
            issue_retest(self.path, 'night', 'P')
        self.accept('other')
        with self.assertRaises(ConflictError):
            issue_retest(self.path, 'other', 'P', ('y', 'x'))
        with self.assertRaises(NotFound):
            issue_retest(self.path, 'absent', 'T')
        with self.assertRaises(NotFound):
            read_retest(self.path, 'absent')

    def test_issue_selection_validation(self):
        self.night()
        with self.assertRaises(ValueError):
            issue_retest(self.path, 'night', 'S', 'x')
        with self.assertRaises(NotFound):
            issue_retest(self.path, 'night', 'S', ('no-field',))
        with self.assertRaises(NotFound):
            read_retest(self.path, 'S')
        with self.assertRaises(ValueError):
            issue_retest(self.path, 'night', '')
        with self.assertRaises(ValueError):
            issue_retest(self.path, '', 'S')

    def test_repeat_missing_keeps_original_episode(self):
        self.night()
        amend(self.path, 'night', 'repeat-missing', (('R-20', 'x', MISSING),))
        ticket = issue_retest(self.path, 'night', 'P2', ('x',))
        self.assertEqual(ticket['items'][0]['opened'].kind, 'original')
        self.assertIsNone(ticket['items'][0]['opened'].request_id)

    def test_ordinary_correction_closes_and_reopens(self):
        self.open_episodes()
        p = read_retest(self.path, 'P')
        self.assertEqual(tuple(item['status'] for item in p['items']), ('corrected', 'pending'))
        self.assertEqual(p['items'][0]['closed_by'].request_id, 'ordinary-fill')
        self.assertEqual(read_retest(self.path, 'P2')['items'][0]['status'], 'corrected')
        self.assertEqual(tuple(item['status'] for item in read_retest(self.path, 'Q')['items']),
                         ('pending', 'pending'))
        reclaimed = issue_retest(self.path, 'night', 'Q', ('y', 'x'))
        self.assertEqual(reclaimed['items'][0]['opened'].request_id, 'missing-again')
        self.assertEqual(reclaimed['items'][0]['opened'].kind, 'correction')
        self.assertEqual(reclaimed['items'][1]['opened'].kind, 'original')

    def test_open_ticket_does_not_reserve_fields(self):
        self.night()
        issue_retest(self.path, 'night', 'P', ('x', 'y'))
        receipt = amend(self.path, 'night', 'free', (('R-20', 'x', 3),))
        self.assertEqual(receipt.request_id, 'free')
        self.assertEqual(read_retest(self.path, 'P')['items'][0]['status'], 'corrected')

    def test_rejection_is_whole_batch_and_frozen(self):
        self.open_episodes()
        rejected = return_retest(self.path, 'P', 'R-P', ((1, 5), (2, 4)))
        self.assertEqual(rejected['status'], 'rejected')
        self.assertEqual((rejected['ticket_id'], rejected['return_id']), ('P', 'R-P'))
        self.assertEqual(tuple(item['item_id'] for item in rejected['blocked']), (1,))
        self.assertEqual(rejected['blocked'][0]['status'], 'corrected')
        self.assertEqual(rejected['blocked'][0]['closed_by'].request_id, 'ordinary-fill')
        self.assertEqual(read_retest(self.path, 'P')['items'][1]['status'], 'pending')
        self.assertEqual(return_retest(self.path, 'P', 'R-P', ((2, 4), (1, 5))), rejected)
        with self.assertRaises(ConflictError):
            return_retest(self.path, 'P', 'R-P', ((1, 5),))
        with self.assertRaises(ConflictError):
            return_retest(self.path, 'Q', 'R-P', ((1, 0),))
        with self.assertRaises(ConflictError):
            amend(self.path, 'night', 'R-P', (('R-20', 'y', 4),))
        with self.assertRaises(NotFound):
            resume(self.path, 'R-P')
        return_retest(self.path, 'Q', 'R-Q', ((1, 0), (2, 4)))
        self.assertEqual(read_retest(self.path, 'P')['items'][1]['status'], 'superseded')
        self.assertEqual(return_retest(self.path, 'P', 'R-P', ((1, 5), (2, 4))), rejected)

    def test_return_fills_and_supersedes(self):
        self.open_episodes()
        self.pause_q_return()
        self.assertEqual(tuple(item['status'] for item in read_retest(self.path, 'Q')['items']),
                         ('pending', 'pending'))
        self.assertEqual(tuple(item.value for item in current(self.path, 'night')[:2]), (MISSING, MISSING))
        amend(self.path, 'night', 'shared-word', (('R-20', 'z', 6),))
        with self.assertRaises(FieldsBusy) as busy:
            amend(self.path, 'night', 'same-field', (('R-20', 'x', 5),))
        self.assertEqual(busy.exception.holder, 'R-Q')
        self.assertEqual(reconcile(self.path, 'night').words[0].actual_word, 48)
        initialise_archive(self.path)
        receipt = resume(self.path, 'R-Q')
        self.assertEqual(receipt.request_id, 'R-Q')
        q = read_retest(self.path, 'Q')
        self.assertEqual(tuple(item['status'] for item in q['items']), ('filled', 'filled'))
        self.assertTrue(all(item['closed_by'].request_id == 'R-Q' for item in q['items']))
        self.assertEqual(read_retest(self.path, 'Q2')['items'][0]['status'], 'superseded')
        p = read_retest(self.path, 'P')
        self.assertEqual(tuple(item['status'] for item in p['items']), ('corrected', 'superseded'))
        self.assertEqual(p['items'][1]['closed_by'].request_id, 'R-Q')
        ledger = reconcile(self.path, 'night')
        self.assertEqual(tuple(word.actual_word for word in ledger.words[:2]), (48, 4))
        self.assertTrue(ledger.settled)
        self.assertEqual(frozen(self.path, receipt.report_id).receipt, receipt)
        amend(self.path, 'night', 'later-missing', (('R-20', 'x', MISSING),))
        self.assertEqual(read_retest(self.path, 'Q'), q)
        self.assertEqual(return_retest(self.path, 'Q', 'R-Q', ((2, 4), (1, 0))), receipt)
        self.assertEqual(resume(self.path, 'R-Q'), receipt)
        anchored = issue_retest(self.path, 'night', 'Q3', ('x',))
        self.assertEqual(anchored['items'][0]['opened'].request_id, 'later-missing')

    def test_return_cancel_keeps_episodes_open(self):
        self.open_episodes()
        self.pause_q_return()
        amend(self.path, 'night', 'shared-word', (('R-20', 'z', 6),))
        initialise_archive(self.path)
        cancelled = resume(self.path, 'R-Q', action='cancel')
        self.assertEqual(cancelled, CancelledCorrection('R-Q', 'night'))
        ledger = reconcile(self.path, 'night')
        self.assertEqual(tuple(word.actual_word for word in ledger.words[:2]), (55, 7))
        self.assertTrue(ledger.settled)
        self.assertEqual(tuple(item['status'] for item in read_retest(self.path, 'Q')['items']),
                         ('pending', 'pending'))
        self.assertEqual(return_retest(self.path, 'Q', 'R-Q', ((1, 0), (2, 4))), cancelled)
        self.assertEqual(resume(self.path, 'R-Q'), cancelled)
        receipt = return_retest(self.path, 'Q', 'R-Q2', ((1, 0), (2, 4)))
        self.assertEqual(tuple(item['status'] for item in read_retest(self.path, 'Q')['items']),
                         ('filled', 'filled'))
        self.assertEqual(read_retest(self.path, 'Q')['items'][0]['closed_by'].request_id, 'R-Q2')
        self.assertEqual(frozen(self.path, receipt.report_id).receipt, receipt)

    def test_return_publication_reply_lost(self):
        self.open_episodes()
        with self.assertRaises(ReplyLost):
            return_retest(self.path, 'Q', 'R-Q', ((1, 0), (2, 4)), hook=lose_at('after_publish'))
        receipt = return_retest(self.path, 'Q', 'R-Q', ((1, 0), (2, 4)))
        self.assertEqual(receipt.request_id, 'R-Q')
        self.assertEqual(tuple(item['status'] for item in read_retest(self.path, 'Q')['items']),
                         ('filled', 'filled'))
        self.assertEqual(resume(self.path, 'R-Q'), receipt)
        self.assertEqual(frozen(self.path, receipt.report_id).receipt, receipt)

    def test_return_validation_and_busy_do_not_bind(self):
        self.open_episodes()
        with self.assertRaises(ValueError):
            return_retest(self.path, 'Q', 'R-bad', ())
        for bad_items in (((1,),), ((1, 2, 3),), ((0, 1),), ((-1, 1),), ((True, 1),), ((1, MISSING),),
                          ((1, True),), ((1, -1),), ((1, 7),), ((1, 8),), ((1, 0), (1, 1)), 'x'):
            with self.subTest(bad_items=bad_items), self.assertRaises(ValueError):
                return_retest(self.path, 'Q', 'R-bad', bad_items)
            with self.assertRaises(NotFound):
                resume(self.path, 'R-bad')
        with self.assertRaises(NotFound):
            return_retest(self.path, 'absent', 'R-bad', ((1, 0),))
        with self.assertRaises(NotFound):
            return_retest(self.path, 'Q', 'R-bad', ((9, 0),))
        with self.assertRaises(NotFound):
            resume(self.path, 'R-bad')
        self.pause_q_return()
        with self.assertRaises(FieldsBusy):
            return_retest(self.path, 'Q2', 'R-Q2', ((1, 5),))
        with self.assertRaises(NotFound):
            resume(self.path, 'R-Q2')
        resume(self.path, 'R-Q')

    def test_mutual_exclusion_with_ordinary_requests(self):
        self.open_episodes()
        amend(self.path, 'night', 'ordinary', (('R-20', 'z', 5),))
        with self.assertRaises(ConflictError):
            return_retest(self.path, 'Q', 'ordinary', ((1, 0),))
        receipt = return_retest(self.path, 'Q', 'R-Q', ((1, 0), (2, 4)))
        with self.assertRaises(ConflictError):
            amend(self.path, 'night', 'R-Q', (('R-20', 'x', 0), ('R-20', 'y', 4)))
        self.assertEqual(resume(self.path, 'R-Q'), receipt)

    def test_issue_and_formal_read_during_prewrite(self):
        self.open_episodes()
        self.pause_q_return()
        ticket = issue_retest(self.path, 'night', 'DURING', ('x', 'y'))
        self.assertEqual(tuple(item['opened'].request_id for item in ticket['items']), ('missing-again', None))
        self.assertEqual(tuple(item.value for item in current(self.path, 'night')[:2]), (MISSING, MISSING))
        resume(self.path, 'R-Q')
        self.assertEqual(tuple(item['status'] for item in read_retest(self.path, 'DURING')['items']),
                         ('superseded', 'superseded'))

    def test_old_interfaces_intact(self):
        self.open_episodes()
        original = frozen(self.path, 'original:night')
        self.assertEqual(tuple(item.value for item in original.observations[:3]), (MISSING, MISSING, 2))
        receipt = return_retest(self.path, 'Q', 'R-Q', ((1, 0), (2, 4)))
        initialise_archive(self.path)
        self.assertEqual(frozen(self.path, 'original:night'), original)
        report = frozen(self.path, receipt.report_id)
        self.assertEqual(tuple(item.value for item in report.observations), (0, 4))
        self.assertEqual(tuple(item.value for item in current(self.path, 'night')[:3]), (0, 4, 2))
        self.assertTrue(reconcile(self.path, 'night').settled)
        self.assertEqual(read_retest(self.path, 'Q')['items'][0]['status'], 'filled')
