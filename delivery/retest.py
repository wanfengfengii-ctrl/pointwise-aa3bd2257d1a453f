"""Retest tickets: slips that bind one missing episode per selected field.

A ticket is issued against the formal cross-section. Each item anchors the
continuous missing episode of one original field, identified by its starting
provenance: the sealed original, or the first published correction that signed
the field into this episode. Consecutive MISSING signs keep the same anchor.
The first published real value closes the episode for every ticket listing it.
"""
import json
from dataclasses import dataclass

from archive_db import ConflictError, NotFound, connection, require_name, transaction
from corrections.journal import published_field_history
from delivery.provenance import FieldProvenance
from delivery.selection import resolve_selection
from intake.archive import get_original
from intake.origin import build_origin_index, locate_original_field
from layout.codec import decode_field
from layout.fields import ordered_fields
from layout.registry import get_layout
from layout.values import MISSING

ORIGINAL_EPISODE = (None, None)


@dataclass(frozen=True)
class RetestItem:
    ticket_id: str
    item_id: int
    row_id: str
    field_name: str
    opened_request: str | None
    opened_publication: int | None

    @property
    def opened_key(self):
        return (self.opened_request, self.opened_publication)


@dataclass(frozen=True)
class RetestTicket:
    ticket_id: str
    volume_id: str
    fields: tuple
    items: tuple

    @property
    def items_by_id(self):
        return {item.item_id: item for item in self.items}


def initialise_retest_tickets(db):
    db.execute('''CREATE TABLE IF NOT EXISTS retest_tickets (
        ticket_id TEXT PRIMARY KEY,
        volume_id TEXT NOT NULL REFERENCES originals(volume_id),
        fields_body TEXT NOT NULL
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS retest_items (
        ticket_id TEXT NOT NULL REFERENCES retest_tickets(ticket_id),
        item_id INTEGER NOT NULL,
        row_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        opened_request TEXT,
        opened_publication INTEGER,
        PRIMARY KEY (ticket_id, item_id)
    )''')


def _episode_start(original_value, history):
    """Start of the still-open missing episode: None while a real value stands,
    ORIGINAL_EPISODE for the sealed original, else (request_id, publication)."""
    value = original_value
    start = ORIGINAL_EPISODE if original_value is MISSING else None
    for target, request_id, publication in history:
        if target is MISSING:
            if value is not MISSING:
                start = (request_id, publication)
        else:
            start = None
        value = target
    return start


def _disposition(original_value, history, opened):
    """('pending', None) while the opened episode stands, else ('closed', closer)."""
    value = original_value
    start = ORIGINAL_EPISODE if original_value is MISSING else None
    for target, request_id, publication in history:
        if target is MISSING:
            if value is not MISSING:
                start = (request_id, publication)
        else:
            if start == opened:
                return 'closed', (request_id, publication)
            start = None
        value = target
    if start == opened:
        return 'pending', None
    raise RuntimeError('retest item episode is not anchored in the journal')


def _field_context(original, layout, origins, row_id, field_name):
    origin = locate_original_field(origins, row_id, field_name)
    row = next(entry for entry in original.rows if entry.row_id == row_id)
    field = next(entry for entry in layout.fields if entry.name == field_name)
    return origin, decode_field(row.words[origin.address.slot], field)


def _provenance(origin, key):
    request_id, publication = key
    if request_id is None:
        return FieldProvenance(origin)
    return FieldProvenance(origin, request_id, publication)


def load_ticket(db, ticket_id):
    row = db.execute('SELECT volume_id, fields_body FROM retest_tickets WHERE ticket_id = ?',
                     (ticket_id,)).fetchone()
    if row is None:
        raise NotFound(f'unknown retest ticket {ticket_id}')
    items = tuple(RetestItem(ticket_id, entry['item_id'], entry['row_id'], entry['field_name'],
                             entry['opened_request'], entry['opened_publication'])
                  for entry in db.execute(
                      'SELECT * FROM retest_items WHERE ticket_id = ? ORDER BY item_id', (ticket_id,)))
    return RetestTicket(ticket_id, row['volume_id'], tuple(json.loads(row['fields_body'])), items)


def item_status(db, original, layout, item):
    """(status, closing FieldProvenance or None) for one stored ticket item."""
    origins = build_origin_index(original.volume_id, layout, original.rows)
    origin, original_value = _field_context(original, layout, origins, item.row_id, item.field_name)
    history = published_field_history(db, origin.address, item.field_name)
    state, closer = _disposition(original_value, history, item.opened_key)
    if state == 'pending':
        return 'pending', None
    request_id, publication = closer
    binding = db.execute("""SELECT ticket_id, items_body FROM retest_returns
        WHERE request_id = ? AND outcome = 'accepted'""", (request_id,)).fetchone()
    status = 'corrected'
    if binding is not None:
        used = {int(item_id) for item_id, _reading in json.loads(binding['items_body'])}
        status = 'filled' if binding['ticket_id'] == item.ticket_id and item.item_id in used else 'superseded'
    return status, FieldProvenance(origin, request_id, publication)


def _ticket_dict(ticket, original, layout):
    origins = build_origin_index(original.volume_id, layout, original.rows)
    items = tuple({'item_id': item.item_id, 'row_id': item.row_id, 'field_name': item.field_name,
                   'opened': _provenance(locate_original_field(origins, item.row_id, item.field_name),
                                         item.opened_key)}
                  for item in ticket.items)
    return {'ticket_id': ticket.ticket_id, 'volume_id': ticket.volume_id, 'items': items}


def issue_retest(path, volume_id, ticket_id, fields=None):
    require_name(volume_id, 'volume id')
    require_name(ticket_id, 'ticket id')
    with connection(path) as db, transaction(db, write=True):
        try:
            ticket = load_ticket(db, ticket_id)
        except NotFound:
            ticket = None
        if ticket is not None:
            if ticket.volume_id != volume_id:
                raise ConflictError(f'ticket already bound: {ticket_id}')
            original = get_original(db, ticket.volume_id)
            layout = get_layout(db, original.layout_id)
            if tuple(field.name for field in ordered_fields(layout, fields)) != ticket.fields:
                raise ConflictError(f'ticket already bound: {ticket_id}')
            return _ticket_dict(ticket, original, layout)
        original = get_original(db, volume_id)
        layout = get_layout(db, original.layout_id)
        selected = tuple(field.name for field in ordered_fields(layout, fields))
        origins = build_origin_index(original.volume_id, layout, original.rows)
        found = []
        for key in resolve_selection(original, layout, fields):
            origin, original_value = _field_context(original, layout, origins, key.row_id, key.field_name)
            start = _episode_start(original_value, published_field_history(db, origin.address, key.field_name))
            if start is not None:
                found.append((key.row_id, key.field_name, *start))
        db.execute('INSERT INTO retest_tickets VALUES (?, ?, ?)', (ticket_id, volume_id, json.dumps(selected)))
        db.executemany('INSERT INTO retest_items VALUES (?, ?, ?, ?, ?, ?)',
                       ((ticket_id, number, row_id, field_name, opened_request, opened_publication)
                        for number, (row_id, field_name, opened_request, opened_publication)
                        in enumerate(found, start=1)))
        return _ticket_dict(load_ticket(db, ticket_id), original, layout)


def read_retest(path, ticket_id):
    require_name(ticket_id, 'ticket id')
    with connection(path) as db, transaction(db):
        ticket = load_ticket(db, ticket_id)
        original = get_original(db, ticket.volume_id)
        layout = get_layout(db, original.layout_id)
        result = _ticket_dict(ticket, original, layout)
        items = []
        for entry, item in zip(result['items'], ticket.items, strict=True):
            status, closed_by = item_status(db, original, layout, item)
            items.append({**entry, 'status': status, 'closed_by': closed_by})
        return {'ticket_id': ticket.ticket_id, 'volume_id': ticket.volume_id, 'items': tuple(items)}
