"""Return bindings for retest tickets, sharing the correction request id space."""
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ReturnBinding:
    request_id: str
    ticket_id: str
    items: tuple
    outcome: str
    blocked: tuple | None


def initialise_retest_returns(db):
    db.execute('''CREATE TABLE IF NOT EXISTS retest_returns (
        request_id TEXT PRIMARY KEY,
        ticket_id TEXT NOT NULL REFERENCES retest_tickets(ticket_id),
        items_body TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'rejected')),
        blocked_body TEXT
    )''')


def load_return(db, request_id):
    row = db.execute('SELECT * FROM retest_returns WHERE request_id = ?', (request_id,)).fetchone()
    if row is None:
        return None
    items = tuple((int(item_id), reading) for item_id, reading in json.loads(row['items_body']))
    blocked = None
    if row['blocked_body'] is not None:
        blocked = tuple((int(item_id), status, request, publication)
                        for item_id, status, request, publication in json.loads(row['blocked_body']))
    return ReturnBinding(row['request_id'], row['ticket_id'], items, row['outcome'], blocked)


def record_return(db, request_id, ticket_id, items, outcome, blocked=None):
    body = json.dumps([[item_id, reading] for item_id, reading in items])
    blocked_body = None if blocked is None else json.dumps([list(entry) for entry in blocked])
    db.execute('INSERT INTO retest_returns VALUES (?, ?, ?, ?, ?)',
               (request_id, ticket_id, body, outcome, blocked_body))
