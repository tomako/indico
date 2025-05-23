# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from flask import session

from indico.core import signals
from indico.core.db import db
from indico.core.db.sqlalchemy.util.session import no_autoflush
from indico.modules.attachments.models.attachments import Attachment, AttachmentFile, AttachmentType
from indico.modules.attachments.models.folders import AttachmentFolder
from indico.modules.events.abstracts.settings import SubmissionRightsType
from indico.modules.events.contributions import contribution_settings, logger
from indico.modules.events.contributions.models.contributions import Contribution
from indico.modules.events.contributions.models.persons import AuthorType, ContributionPersonLink
from indico.modules.events.contributions.models.subcontributions import SubContribution
from indico.modules.events.timetable.operations import (delete_timetable_entry, schedule_contribution,
                                                        update_timetable_entry)
from indico.modules.events.util import format_log_person, format_log_ref, set_custom_fields, split_log_location_changes
from indico.modules.logs.models.entries import EventLogRealm, LogKind
from indico.modules.logs.util import make_diff_log
from indico.util.signals import make_interceptable


def _ensure_consistency(contrib):
    """Unschedule contribution if not consistent with timetable.

    A contribution that has no session assigned, may not be scheduled
    inside a session.  A contribution that has a session assigned may
    only be scheduled inside a session block associated with that
    session, and that session block must match the session block of
    the contribution.

    :return: A bool indicating whether the contribution has been
             unscheduled to preserve consistency.
    """
    entry = contrib.timetable_entry
    if entry is None:
        return False
    if entry.parent_id is None and (contrib.session is not None or contrib.session_block is not None):
        # Top-level entry but we have a session/block set
        delete_timetable_entry(entry, log=False)
        return True
    elif entry.parent_id is not None:
        parent = entry.parent
        # Nested entry but no or a different session/block set
        if parent.session_block.session != contrib.session or parent.session_block != contrib.session_block:
            delete_timetable_entry(entry, log=False)
            return True
    return False


@make_interceptable
def create_contribution(event, contrib_data, custom_fields_data=None, session_block=None, extend_parent=False):
    user = session.user if session else None
    start_dt = contrib_data.pop('start_dt', None)
    contrib = Contribution(event=event)
    contrib.populate_from_dict(contrib_data)
    if custom_fields_data:
        set_custom_fields(contrib, custom_fields_data)
    db.session.flush()
    if start_dt is not None:
        schedule_contribution(contrib, start_dt=start_dt, session_block=session_block, extend_parent=extend_parent)
    signals.event.contribution_created.send(contrib, cloned_from=None)
    logger.info('Contribution %s created by %s', contrib, user)
    contrib.log(EventLogRealm.management, LogKind.positive, 'Contributions',
                f'Contribution {contrib.verbose_title} has been created', user)
    # Note: If you ever add more stuff here that should run for any new contribution, make sure
    # to also add it to ContributionCloner.clone_single_contribution
    return contrib


@no_autoflush
def update_contribution(contrib: Contribution, contrib_data: dict, custom_fields_data=None):
    """Update a contribution.

    :param contrib: The `Contribution` to update
    :param contrib_data: A dict containing the data to update
    :param custom_fields_data: A dict containing the data for custom
                               fields.
    :return: A dictionary containing information related to the
             update.  `unscheduled` will be true if the modification
             resulted in the contribution being unscheduled.  In this
             case `undo_unschedule` contains the necessary data to
             re-schedule it (undoing the session change causing it to
             be unscheduled)
    """
    rv = {'unscheduled': False, 'undo_unschedule': None}
    current_session_block = contrib.session_block
    start_dt = contrib_data.pop('start_dt', None)
    if start_dt is not None:
        update_timetable_entry(contrib.timetable_entry, {'start_dt': start_dt})
    old_person_links = contrib.sorted_person_links[:]
    changes = contrib.populate_from_dict(contrib_data)
    changes.pop('person_link_data', None)
    visible_person_link_changes = contrib.sorted_person_links != old_person_links
    if visible_person_link_changes or 'person_link_data' in contrib_data:
        changes['person_links'] = (old_person_links, contrib.sorted_person_links)
    if custom_fields_data:
        changes.update(set_custom_fields(contrib, custom_fields_data))
    if 'session' in contrib_data:
        timetable_entry = contrib.timetable_entry
        if timetable_entry is not None and _ensure_consistency(contrib):
            rv['unscheduled'] = True
            rv['undo_unschedule'] = {'start_dt': timetable_entry.start_dt.isoformat(),
                                     'contribution_id': contrib.id,
                                     'session_block_id': current_session_block.id if current_session_block else None,
                                     'force': True}
    db.session.flush()
    if changes:
        signals.event.contribution_updated.send(contrib, changes=changes)
        logger.info('Contribution %s updated by %s', contrib, session.user)
        log_contribution_update(contrib, changes, visible_person_link_changes=visible_person_link_changes)
    return rv


def delete_contribution(contrib):
    contrib.is_deleted = True
    if contrib.timetable_entry is not None:
        delete_timetable_entry(contrib.timetable_entry, log=False)
    db.session.flush()
    signals.event.contribution_deleted.send(contrib)
    logger.info('Contribution %s deleted by %s', contrib, session.user)
    contrib.log(EventLogRealm.management, LogKind.negative, 'Contributions',
                f'Contribution {contrib.verbose_title} has been deleted', session.user)


def create_subcontribution(contrib, data):
    subcontrib = SubContribution()
    subcontrib.populate_from_dict(data)
    contrib.subcontributions.append(subcontrib)
    db.session.flush()
    signals.event.subcontribution_created.send(subcontrib, cloned_from=None)
    logger.info('Subcontribution %s created by %s', subcontrib, session.user)
    subcontrib.event.log(EventLogRealm.management, LogKind.positive, 'Subcontributions',
                         f'Subcontribution "{subcontrib.title}" has been created', session.user,
                         meta={'subcontribution_id': subcontrib.id})
    return subcontrib


def update_subcontribution(subcontrib, data):
    subcontrib.populate_from_dict(data)
    db.session.flush()
    signals.event.subcontribution_updated.send(subcontrib)
    logger.info('Subcontribution %s updated by %s', subcontrib, session.user)
    subcontrib.event.log(EventLogRealm.management, LogKind.change, 'Subcontributions',
                         f'Subcontribution "{subcontrib.title}" has been updated', session.user,
                         meta={'subcontribution_id': subcontrib.id})


def delete_subcontribution(subcontrib):
    subcontrib.is_deleted = True
    db.session.flush()
    signals.event.subcontribution_deleted.send(subcontrib)
    logger.info('Subcontribution %s deleted by %s', subcontrib, session.user)
    subcontrib.event.log(EventLogRealm.management, LogKind.negative, 'Subcontributions',
                         f'Subcontribution "{subcontrib.title}" has been deleted', session.user,
                         meta={'subcontribution_id': subcontrib.id})


@no_autoflush
def create_contribution_from_abstract(abstract, contrib_session=None):
    from indico.modules.events.abstracts.settings import abstracts_settings

    event = abstract.event
    contrib_person_links = set()
    person_link_attrs = {'_title', 'address', 'affiliation', 'first_name', 'last_name', 'phone', 'author_type',
                         'is_speaker', 'display_order'}
    for abstract_person_link in abstract.person_links:
        link = ContributionPersonLink(person=abstract_person_link.person)
        link.populate_from_attrs(abstract_person_link, person_link_attrs)
        contrib_person_links.add(link)
    if contrib_session:
        duration = contrib_session.default_contribution_duration
    else:
        duration = contribution_settings.get(event, 'default_duration')
    all_authors_can_submit = (event.cfa.contribution_submitters == SubmissionRightsType.all)
    primary_authors_can_submit = (event.cfa.contribution_submitters == SubmissionRightsType.speakers_primary)
    person_link_data = {link: (all_authors_can_submit or
                               (primary_authors_can_submit and link.author_type == AuthorType.primary) or
                               link.is_speaker)
                        for link in contrib_person_links}
    custom_fields_data = {f'custom_{field_value.contribution_field.id}': field_value.data for
                          field_value in abstract.field_values}
    contrib = create_contribution(event, {'friendly_id': abstract.friendly_id,
                                          'title': abstract.title,
                                          'duration': duration,
                                          'description': abstract.description,
                                          'type': abstract.accepted_contrib_type,
                                          'track': abstract.accepted_track,
                                          'session': contrib_session,
                                          'person_link_data': person_link_data},
                                  custom_fields_data=custom_fields_data)
    if abstracts_settings.get(event, 'copy_attachments') and abstract.files:
        folder = AttachmentFolder.get_or_create_default(contrib)
        for abstract_file in abstract.files:
            attachment = Attachment(user=abstract.submitter, type=AttachmentType.file, folder=folder,
                                    title=abstract_file.filename)
            attachment.file = AttachmentFile(user=abstract.submitter, filename=abstract_file.filename,
                                             content_type=abstract_file.content_type)
            with abstract_file.open() as fd:
                attachment.file.save(fd)
    db.session.flush()
    return contrib


def log_contribution_update(contrib, changes, *, visible_person_link_changes=False):
    if not changes:
        return

    log_fields = {
        'title': {'title': 'Title', 'type': 'string'},
        'description': 'Description',
        'address': 'Address',
        'venue_room': {'title': 'Location', 'type': 'string'},
        'keywords': 'Keywords',
        'board_number': {'title': 'Board number', 'type': 'string'},
        'code': {'title': 'Program code', 'type': 'string'},
        'duration': 'Duration',
        'protection_mode': 'Protection mode',
        'references': {
            'title': 'External IDs',
            'convert': lambda changes: [list(map(format_log_ref, refs)) for refs in changes]
        },
        'person_links': {
            'title': 'Persons',
            'convert': lambda changes: [list(map(format_log_person, persons)) for persons in changes]
        },
        'track': {
            'title': 'Track',
            'type': 'string',
            'convert': lambda changes: [x.full_title if x else None for x in changes]
        },
        'session': {
            'title': 'Session',
            'type': 'string',
            'convert': lambda changes: [x.title if x else None for x in changes]
        },
        'session_block': {
            'title': 'Session Block',
            'type': 'string',
            'convert': lambda changes: [x.full_title if x else None for x in changes]
        },
        'type': {
            'title': 'Type',
            'type': 'string',
            'convert': lambda changes: [x.name if x else None for x in changes]
        },
    }
    changes = split_log_location_changes(changes)
    if not visible_person_link_changes:
        # Don't log a person link change with no visible changes (changes
        # on an existing link or reordering). It would look quite weird in
        # the event log.
        # TODO: maybe use a separate signal for such changes to log them
        # anyway and allow other code to act on them?
        changes.pop('person_links', None)

    for field_name in changes:
        if not field_name.startswith('custom_') or not any(changes):
            continue
        field_id = int(field_name.removeprefix('custom_'))
        field = contrib.event.get_contribution_field(field_id)
        field_impl = field.field
        log_fields[field_name] = {
            'title': field.title,
            'type': field_impl.log_type,
            'convert': lambda change, field_impl=field_impl: list(map(field_impl.get_friendly_value, change))
        }
    if changes:
        extra = ''
        if len(changes) == 1:
            what = log_fields[list(changes.keys())[0]]
            if isinstance(what, dict):
                what = what['title']
            extra = f' ({what})'
        contrib.log(EventLogRealm.management, LogKind.change, 'Contributions',
                    f'Contribution {contrib.verbose_title} has been updated{extra}', session.user,
                    data={'Changes': make_diff_log(changes, log_fields)})
