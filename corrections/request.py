from archive_db import ConflictError, NotFound, connection, require_name, transaction
from corrections.journal import FieldEvidence, canonical_request, load_request, match_request, record_pending
from corrections.pending import reserve_fields
from corrections.recover import advance_correction
from corrections.retest import load_return, record_return
from delivery.current import read_current_fields
from delivery.provenance import FieldProvenance
from delivery.retest import item_status, load_ticket
from delivery.selection import ObservationKey
from intake.archive import get_original
from intake.origin import build_origin_index, locate_original_field
from layout.fields import resolve_field
from layout.registry import get_layout
from layout.values import MISSING, encode_value


def validate_targets(original, layout, targets):
    origins = build_origin_index(original.volume_id, layout, original.rows)
    located = []
    for row_id, name, value in targets:
        field = resolve_field(layout, name)
        origin = locate_original_field(origins, row_id, name)
        encode_value(field, value)
        located.append((origin, value))
    return tuple(sorted(located, key=lambda item: (item[0].row_number, item[0].address.slot)))


def _admit(db, request_id, original, layout, targets):
    located = validate_targets(original, layout, targets)
    keys = tuple(ObservationKey(origin.address.row_id, origin.field_name) for origin, value in located)
    observations = read_current_fields(db, original, layout, keys)
    evidence = tuple(FieldEvidence(request_id, origin.address, origin.field_name, observation.value,
                                   value, observation.provenance.request_id, observation.provenance.publication)
                     for (origin, value), observation in zip(located, observations, strict=True))
    record_pending(db, request_id, original.volume_id, evidence)
    reserve_fields(db, request_id, evidence)


def amend(path, volume_id, request_id, changes, *, hook=None):
    require_name(volume_id, 'volume id')
    require_name(request_id, 'request id')
    targets = canonical_request(changes)
    with connection(path) as db, transaction(db, write=True):
        if load_return(db, request_id) is not None:
            raise ConflictError(f'request already bound: {request_id}')
        try:
            prior = load_request(db, request_id)
        except NotFound:
            original = get_original(db, volume_id)
            layout = get_layout(db, original.layout_id)
            _admit(db, request_id, original, layout, targets)
        else:
            match_request(prior, volume_id, targets)
    return advance_correction(path, request_id, hook=hook)


def canonical_return(items):
    if isinstance(items, (str, bytes)) or not hasattr(items, '__iter__'):
        raise ValueError('retest return items must be a sequence')
    pairs = {}
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError('every return must contain an item id and a reading')
        item_id, reading = item
        if type(item_id) is not int or item_id < 1:
            raise ValueError('item id must be a positive integer')
        if reading is MISSING:
            raise ValueError('a retest return needs a real reading')
        if type(reading) is not int or reading < 0:
            raise ValueError('expected a nonnegative integer reading')
        if item_id in pairs:
            raise ValueError(f'repeated return item {item_id}')
        pairs[item_id] = reading
    if not pairs:
        raise ValueError('a retest return needs at least one item')
    return tuple(sorted(pairs.items()))


def _closed_items(db, ticket, original, layout, canonical):
    known = ticket.items_by_id
    blocked = []
    for item_id, reading in canonical:
        if item_id not in known:
            raise NotFound(f'unknown retest item {ticket.ticket_id}/{item_id}')
        item = known[item_id]
        encode_value(resolve_field(layout, item.field_name), reading)
        status, closed_by = item_status(db, original, layout, item)
        if status != 'pending':
            blocked.append((item_id, status, closed_by.request_id, closed_by.publication))
    return tuple(blocked)


def _rejection(binding, ticket, original, layout):
    origins = build_origin_index(original.volume_id, layout, original.rows)
    known = ticket.items_by_id
    blocked = []
    for item_id, status, request_id, publication in binding.blocked:
        item = known[item_id]
        origin = locate_original_field(origins, item.row_id, item.field_name)
        blocked.append({'item_id': item_id, 'status': status,
                        'closed_by': FieldProvenance(origin, request_id, publication)})
    return {'status': 'rejected', 'ticket_id': binding.ticket_id,
            'return_id': binding.request_id, 'blocked': tuple(blocked)}


def return_retest(path, ticket_id, return_id, items, *, hook=None):
    require_name(ticket_id, 'ticket id')
    require_name(return_id, 'return id')
    canonical = canonical_return(items)
    with connection(path) as db, transaction(db, write=True):
        binding = load_return(db, return_id)
        if binding is not None:
            if binding.ticket_id != ticket_id or binding.items != canonical:
                raise ConflictError(f'request already bound: {return_id}')
            if binding.outcome == 'rejected':
                ticket = load_ticket(db, ticket_id)
                original = get_original(db, ticket.volume_id)
                layout = get_layout(db, original.layout_id)
                return _rejection(binding, ticket, original, layout)
        else:
            try:
                load_request(db, return_id)
            except NotFound:
                pass
            else:
                raise ConflictError(f'request already bound: {return_id}')
            ticket = load_ticket(db, ticket_id)
            original = get_original(db, ticket.volume_id)
            layout = get_layout(db, original.layout_id)
            blocked = _closed_items(db, ticket, original, layout, canonical)
            if blocked:
                record_return(db, return_id, ticket_id, canonical, 'rejected', blocked)
                return _rejection(load_return(db, return_id), ticket, original, layout)
            targets = canonical_request(tuple((ticket.items_by_id[item_id].row_id,
                                               ticket.items_by_id[item_id].field_name, reading)
                                              for item_id, reading in canonical))
            _admit(db, return_id, original, layout, targets)
            record_return(db, return_id, ticket_id, canonical, 'accepted')
    return advance_correction(path, return_id, hook=hook)
