import json
from pathlib import Path
from tempfile import TemporaryDirectory

from archive_db import ReplyLost
from corrections.pending import FieldsBusy
from corrections.recover import resume
from corrections.request import amend
from delivery.current import current
from delivery.reconcile import reconcile
from delivery.snapshot import frozen
from demo import LAYOUT, MANIFEST, ROWS, lose_at
from intake.register import ingest, initialise_archive
from intake.validate import InputRow
from layout.registry import define
from layout.values import MISSING


VOLUME = "night"
ROW = "R-20"
RETEST_ROWS = (InputRow(ROW, 4, 2, (23, 7)), ROWS[1])


def seed(path):
    initialise_archive(path)
    define(path, LAYOUT)
    return ingest(path, VOLUME, LAYOUT.layout_id, RETEST_ROWS, MANIFEST)


def x_observation(path):
    return current(path, VOLUME, ("x",))[0]


def exercise_v1(path):
    accepted = seed(path)
    original = frozen(path, accepted.report_id)
    assert x_observation(path).value is MISSING
    repeat = amend(path, VOLUME, "repeat-missing", ((ROW, "x", MISSING),))
    assert x_observation(path).provenance.request_id == repeat.request_id
    fill = amend(path, VOLUME, "ordinary-fill", ((ROW, "x", 2),))
    assert x_observation(path).value == 2
    again = amend(path, VOLUME, "missing-again", ((ROW, "x", MISSING),))
    assert x_observation(path).value is MISSING
    assert x_observation(path).provenance.request_id == again.request_id
    assert frozen(path, fill.report_id).observations[0].value == 2

    changes = ((ROW, "x", 0), (ROW, "y", 4))
    try:
        amend(path, VOLUME, "paused", changes, hook=lose_at("after_word_commit"))
    except ReplyLost as loss:
        assert (loss.detail.before, loss.detail.after) == (23, 16)
    else:
        raise AssertionError("expected first word reply loss")
    amend(path, VOLUME, "shared-word", ((ROW, "z", 6),))
    paused = current(path, VOLUME)[:3]
    assert tuple(item.value for item in paused) == (MISSING, MISSING, 6)
    assert reconcile(path, VOLUME).words[0].actual_word == 48
    try:
        amend(path, VOLUME, "same-field", ((ROW, "x", 5),))
    except FieldsBusy as busy:
        assert busy.holder == "paused"
    else:
        raise AssertionError("expected busy field")
    initialise_archive(path)
    resume(path, "paused", action="cancel")
    cancelled = reconcile(path, VOLUME)
    assert cancelled.settled
    assert tuple(word.actual_word for word in cancelled.words[:2]) == (55, 7)

    try:
        amend(path, VOLUME, "zero-fill", changes, hook=lose_at("after_publish"))
    except ReplyLost as loss:
        committed = loss.detail
    else:
        raise AssertionError("expected publication reply loss")
    assert x_observation(path).value == 0
    amend(path, VOLUME, "later-missing", ((ROW, "x", MISSING),))
    assert resume(path, "zero-fill") == committed
    assert x_observation(path).provenance.request_id == "later-missing"
    assert frozen(path, accepted.report_id) == original
    return {
        "first_fill": fill.request_id,
        "first_fill_publication": fill.publication,
        "later_missing_publication": again.publication,
        "paused_word": 48,
        "cancelled_words": [word.actual_word for word in cancelled.words[:2]],
        "zero_fill_receipt": committed.request_id,
        "current_source": x_observation(path).provenance.request_id,
    }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(exercise_v1(Path(directory) / "night.sqlite3"), sort_keys=True))
