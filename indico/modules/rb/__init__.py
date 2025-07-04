# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from datetime import datetime

from flask import session

from indico.core import signals
from indico.core.cache import make_scoped_cache
from indico.core.config import config
from indico.core.db.sqlalchemy.protection import make_acl_log_fn
from indico.core.logger import Logger
from indico.core.permissions import ManagementPermission, check_permissions
from indico.core.settings import SettingsProxy
from indico.core.settings.converters import EnumConverter, ModelListConverter
from indico.modules.categories.models.categories import Category
from indico.modules.rb.models.locations import Location
from indico.modules.rb.models.rooms import Room
from indico.modules.rb.util import rb_check_if_visible
from indico.util.enum import RichIntEnum
from indico.util.i18n import _, pgettext
from indico.web.flask.util import url_for
from indico.web.menu import SideMenuItem, TopMenuItem


logger = Logger.get('rb')

rb_cache = make_scoped_cache('roombooking')

# Log ACL changes
signals.acl.entry_changed.connect(make_acl_log_fn(Location), sender=Location, weak=False)
signals.acl.entry_changed.connect(make_acl_log_fn(Room), sender=Room, weak=False)


class BookingReasonRequiredOptions(RichIntEnum):
    __titles__ = [None, _('Never'), _('Always'), _('Not for events')]

    never = 0
    always = 1
    not_for_events = 2


rb_settings = SettingsProxy('roombooking', {
    'managers_edit_rooms': False,
    'hide_booking_details': False,
    'hide_module_if_unauthorized': False,
    'excluded_categories': [],
    'notification_before_days': 2,
    'notification_before_days_weekly': 5,
    'notification_before_days_monthly': 7,
    'notifications_enabled': True,
    'end_notification_daily': 1,
    'end_notification_weekly': 3,
    'end_notification_monthly': 7,
    'end_notifications_enabled': True,
    'internal_notes_enabled': False,
    'booking_limit': 365,
    'tileserver_url': None,
    'grace_period': None,
    'booking_reason_required': BookingReasonRequiredOptions.always,
}, acls={
    'admin_principals',
    'authorized_principals'
}, converters={
    'excluded_categories': ModelListConverter(Category),
    'booking_reason_required': EnumConverter(BookingReasonRequiredOptions),
})


@signals.core.import_tasks.connect
def _import_tasks(sender, **kwargs):
    import indico.modules.rb.tasks  # noqa: F401


@signals.users.preferences.connect
def _get_extra_user_prefs(sender, **kwargs):
    from indico.modules.rb.user_prefs import RBUserPreferences
    if config.ENABLE_ROOMBOOKING:
        return RBUserPreferences


@signals.menu.items.connect_via('admin-sidemenu')
def _extend_admin_menu(sender, **kwargs):
    if config.ENABLE_ROOMBOOKING and session.user.is_admin:
        url = url_for('rb.roombooking', path='admin')
        return SideMenuItem('rb', _('Room booking'), url, 70, icon='location')


@signals.menu.items.connect_via('top-menu')
def _topmenu_items(sender, **kwargs):
    if config.ENABLE_ROOMBOOKING and rb_check_if_visible(session.user):
        yield TopMenuItem('room_booking', _('Room booking'), url_for('rb.roombooking'), 80)


@signals.menu.items.connect_via('event-management-sidemenu')
def _sidemenu_items(sender, event, **kwargs):
    if config.ENABLE_ROOMBOOKING and event.can_manage(session.user):
        yield SideMenuItem('room_booking', _('Room bookings'), url_for('rb.event_booking_list', event), 50,
                           icon='location')


@signals.users.merged.connect
def _merge_users(target, source, **kwargs):
    from indico.modules.rb.models.blocking_principals import BlockingPrincipal
    from indico.modules.rb.models.blockings import Blocking
    from indico.modules.rb.models.principals import LocationPrincipal, RoomPrincipal
    from indico.modules.rb.models.reservations import Reservation
    Blocking.query.filter_by(created_by_id=source.id).update({Blocking.created_by_id: target.id})
    BlockingPrincipal.merge_users(target, source, 'blocking')
    Reservation.query.filter_by(created_by_id=source.id).update({Reservation.created_by_id: target.id})
    Reservation.query.filter_by(booked_for_id=source.id).update({Reservation.booked_for_id: target.id})
    Room.query.filter_by(owner_id=source.id).update({Room.owner_id: target.id})
    RoomPrincipal.merge_users(target, source, 'room')
    LocationPrincipal.merge_users(target, source, 'location')
    rb_settings.acls.merge_users(target, source)


@signals.event.deleted.connect
def _event_deleted(event, user, **kwargs):
    from indico.modules.rb.models.reservations import ReservationOccurrence
    reservation_occurrence_links = (event.all_room_reservation_occurrence_links
                                    .join(ReservationOccurrence)
                                    .filter(~ReservationOccurrence.is_rejected, ~ReservationOccurrence.is_cancelled)
                                    .all())
    for link in reservation_occurrence_links:
        if link.reservation_occurrence.end_dt >= datetime.now():
            link.reservation_occurrence.cancel(user or session.user, 'Associated event was deleted')


class BookPermission(ManagementPermission):
    name = 'book'
    friendly_name = pgettext('Room booking permission name', 'Book')
    user_selectable = True
    color = 'green'


class PrebookPermission(ManagementPermission):
    name = 'prebook'
    friendly_name = pgettext('Room booking permission name', 'Prebook')
    user_selectable = True
    default = True
    color = 'orange'


class RoomBookPermission(BookPermission):
    description = _('Allows booking the room')


class RoomPrebookPermission(PrebookPermission):
    description = _('Allows prebooking the room')


class LocationBookPermission(BookPermission):
    description = _('Allows booking rooms in the location')


class LocationPrebookPermission(PrebookPermission):
    description = _('Allows prebooking rooms in the location')


class OverridePermission(ManagementPermission):
    name = 'override'
    friendly_name = pgettext('Room booking permission name', 'Override')
    description = _('Allows overriding restrictions when booking the room')
    user_selectable = True
    color = 'pink'


class ModeratePermission(ManagementPermission):
    name = 'moderate'
    friendly_name = pgettext('Room booking permission name', 'Moderate')
    description = _('Allows moderating bookings (approving/rejecting/editing)')
    user_selectable = True
    color = 'purple'


@signals.acl.get_management_permissions.connect_via(Room)
def _get_room_management_permissions(sender, **kwargs):
    yield RoomBookPermission
    yield RoomPrebookPermission
    yield OverridePermission
    yield ModeratePermission


@signals.acl.get_management_permissions.connect_via(Location)
def _get_location_management_permissions(sender, **kwargs):
    yield LocationBookPermission
    yield LocationPrebookPermission
    yield ModeratePermission


@signals.core.app_created.connect
def _check_permissions(app, **kwargs):
    check_permissions(Room)
    check_permissions(Location)
