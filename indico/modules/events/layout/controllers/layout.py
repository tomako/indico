# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import os
from io import BytesIO

from flask import flash, jsonify, redirect, request, session
from PIL import Image
from werkzeug.exceptions import NotFound
from wtforms import fields as wtforms_fields
from wtforms.validators import DataRequired

from indico.core.db import db
from indico.modules.events import EventLogRealm
from indico.modules.events.controllers.base import RegistrationRequired, RHDisplayEventBase
from indico.modules.events.layout import (EVENT_BANNER_WIDTH, EVENT_LOGO_WIDTH, OVERRIDABLE_THEME_SETTINGS,
                                          layout_settings, logger, theme_settings)
from indico.modules.events.layout.forms import (ConferenceLayoutForm, CSSForm, CSSSelectionForm,
                                                LectureMeetingLayoutForm, LogoForm)
from indico.modules.events.layout.util import get_css_file_data, get_css_url, get_js_url, get_logo_data
from indico.modules.events.layout.views import WPLayoutEdit
from indico.modules.events.management.controllers import RHManageEventBase
from indico.modules.events.models.events import EventType
from indico.modules.events.views import WPConferenceDisplay
from indico.modules.logs import LogKind
from indico.modules.logs.util import make_diff_log
from indico.util.fs import secure_filename
from indico.util.i18n import _
from indico.util.signing import secure_serializer
from indico.util.string import crc32
from indico.web.flask.templating import get_template_module
from indico.web.flask.util import send_file, url_for
from indico.web.forms import fields as indico_fields
from indico.web.forms.base import FormDefaults, IndicoForm
from indico.web.util import _pop_injected_js, jsonify_data


class RHLayoutBase(RHManageEventBase):
    def _check_and_flash_logo_warnings(self, logo):
        header_logo_as_banner = layout_settings.get(self.event, 'header_logo_as_banner')
        if header_logo_as_banner and logo.width < EVENT_BANNER_WIDTH * 0.8:
            flash(_('The event logo is too small. It is recommended to use a logo with a width of at least {}px.')
                  .format(EVENT_BANNER_WIDTH), 'warning')
        if not header_logo_as_banner and logo.width > EVENT_LOGO_WIDTH * 1.2:
            flash(_('The event logo is too large. It is recommended to use a logo with a width of at most {}px.')
                  .format(EVENT_LOGO_WIDTH), 'warning')


def _make_theme_settings_form(event, theme):
    try:
        theme_data = theme_settings.themes[theme]
        theme_user_settings = theme_data['user_settings']
    except KeyError:
        return None
    theme_global_settings = theme_data.get('settings', {})
    form_class = type('ThemeSettingsForm', (IndicoForm,), {})
    for name, field_data in theme_user_settings.items():
        field_type = field_data['type']
        field_class = getattr(indico_fields, field_type, None) or getattr(wtforms_fields, field_type, None)
        if not field_class:
            raise Exception(f'Invalid field type: {field_type}')
        label = field_data['caption']
        description = field_data.get('description')
        validators = [DataRequired()] if field_data.get('required') else []
        field = field_class(label, validators, description=description, **field_data.get('kwargs', {}))
        setattr(form_class, name, field)

    defaults = {name: field_data.get('defaults') for name, field_data in theme_user_settings.items()}
    for user_key, theme_keys in OVERRIDABLE_THEME_SETTINGS.items():
        if user_key not in theme_user_settings:
            continue
        defaults[user_key] = all(theme_global_settings.get(key) for key in theme_keys)
    if theme == event.theme:
        defaults.update(layout_settings.get(event, 'timetable_theme_settings'))

    return form_class(csrf_enabled=False, obj=FormDefaults(defaults), prefix='tt-theme-settings-')


class RHLayoutTimetableThemeForm(RHLayoutBase):
    def _process(self):
        form = _make_theme_settings_form(self.event, request.args['theme'])
        if not form:
            return jsonify()
        tpl = get_template_module('forms/_form.html')
        return jsonify(html=tpl.form_rows(form), js=_pop_injected_js())


class RHLayoutEdit(RHLayoutBase):
    def _process_request(self):
        if self.event.type_ == EventType.conference:
            return self._process_conference()
        else:
            return self._process_lecture_meeting()

    def _process_GET(self):
        return self._process_request()

    def _process_POST(self):
        old_values = layout_settings.get_all(self.event)
        ret = self._process_request()
        new_values = layout_settings.get_all(self.event)
        # Skip `timetable_theme_settings` as they are dynamically generated from themes.yaml
        changes = {k: (old_values[k], v) for k, v in new_values.items()
                   if old_values[k] != v and k != 'timetable_theme_settings'}
        if changes:
            form_cls = ConferenceLayoutForm if self.event.type_ == EventType.conference else LectureMeetingLayoutForm
            form = form_cls(event=self.event)
            self.event.log(EventLogRealm.management, LogKind.change, 'Layout', summary='Layout was updated',
                           user=session.user, data={'Changes': make_diff_log(changes, form.log_fields_metadata)})
        return ret

    def _get_form_defaults(self):
        defaults = FormDefaults(**layout_settings.get_all(self.event))
        defaults.timetable_theme = self.event.theme
        return defaults

    def _process_lecture_meeting(self):
        form = LectureMeetingLayoutForm(obj=self._get_form_defaults(), event=self.event)
        tt_theme_settings_form = _make_theme_settings_form(self.event, form.timetable_theme.data)
        tt_form_valid = tt_theme_settings_form.validate_on_submit() if tt_theme_settings_form else True
        if form.validate_on_submit() and tt_form_valid:
            if tt_theme_settings_form:
                layout_settings.set(self.event, 'timetable_theme_settings', tt_theme_settings_form.data)
            else:
                layout_settings.delete(self.event, 'timetable_theme_settings')
            layout_settings.set_multi(self.event, form.data)
            flash(_('Settings saved'), 'success')
            return redirect(url_for('.index', self.event))
        return WPLayoutEdit.render_template('layout_meeting_lecture.html', self.event, form=form,
                                            timetable_theme_settings_form=tt_theme_settings_form)

    def _process_conference(self):
        form = ConferenceLayoutForm(obj=self._get_form_defaults(), event=self.event)
        css_form = CSSForm()
        logo_form = LogoForm()
        tt_theme_settings_form = _make_theme_settings_form(self.event, form.timetable_theme.data)
        tt_form_valid = tt_theme_settings_form.validate_on_submit() if tt_theme_settings_form else True
        if form.validate_on_submit() and tt_form_valid:
            if tt_theme_settings_form:
                layout_settings.set(self.event, 'timetable_theme_settings', tt_theme_settings_form.data)
            else:
                layout_settings.delete(self.event, 'timetable_theme_settings')
            data = {str(key): value for key, value in form.data.items() if key in layout_settings.defaults}
            layout_settings.set_multi(self.event, data)
            if form.theme.data == '_custom':
                layout_settings.set(self.event, 'use_custom_css', True)
            flash(_('Settings saved'), 'success')
            if self.event.has_logo:
                img = Image.open(BytesIO(self.event.logo))
                self._check_and_flash_logo_warnings(img)
            return redirect(url_for('.index', self.event))
        else:
            if self.event.logo_metadata:
                logo_form.logo.data = self.event
            if self.event.has_stylesheet:
                css_form.css_file.data = self.event
        return WPLayoutEdit.render_template('layout_conference.html', self.event, form=form,
                                            logo_form=logo_form, css_form=css_form,
                                            timetable_theme_settings_form=tt_theme_settings_form)


class RHLayoutLogoUpload(RHLayoutBase):
    def _process(self):
        f = request.files['logo']
        try:
            img = Image.open(f)
        except (OSError, Image.DecompressionBombError):
            flash(_('You cannot upload this file as a logo, it may be corrupted or too big.'), 'error')
            return jsonify_data(content=None)
        if img.format.lower() not in {'jpeg', 'png', 'gif'}:
            flash(_('The file has an invalid format ({format})').format(format=img.format), 'error')
            return jsonify_data(content=None)
        if img.mode == 'CMYK':
            flash(_('The logo you uploaded is using the CMYK colorspace and has been converted to RGB. Please check if '
                    'the colors are correct and convert it manually if necessary.'), 'warning')
            img = img.convert('RGB')
        image_bytes = BytesIO()
        img.save(image_bytes, 'PNG')
        image_bytes.seek(0)
        content = image_bytes.read()
        self.event.logo = content
        self.event.logo_metadata = {
            'hash': crc32(content),
            'size': len(content),
            'filename': os.path.splitext(secure_filename(f.filename, 'logo'))[0] + '.png',
            'content_type': 'image/png'
        }
        flash(_('New logo saved'), 'success')
        self._check_and_flash_logo_warnings(img)
        logger.info("New logo '%s' uploaded by %s (%s)", f.filename, session.user, self.event)
        return jsonify_data(content=get_logo_data(self.event))


class RHLayoutLogoDelete(RHLayoutBase):
    def _process(self):
        self.event.logo = None
        self.event.logo_metadata = None
        flash(_('Logo deleted'), 'success')
        logger.info('Logo of %s deleted by %s', self.event, session.user)
        return jsonify_data(content=None)


class RHLayoutCSSUpload(RHLayoutBase):
    def _process(self):
        f = request.files['css_file']
        try:
            self.event.stylesheet = f.read().decode().strip()
        except UnicodeDecodeError:
            flash(_('CSS files must be ASCII or UTF-8 encoded.'), 'error')
            return jsonify_data(success=False, content=None)
        self.event.stylesheet_metadata = {
            'hash': crc32(self.event.stylesheet),
            'size': len(self.event.stylesheet),
            'filename': secure_filename(f.filename, 'stylesheet.css')
        }
        db.session.flush()
        flash(_('New CSS file saved. Do not forget to enable it ("Use custom CSS") after verifying that it is correct '
                'using the preview.'), 'success')
        logger.info('CSS file for %s uploaded by %s', self.event, session.user)
        return jsonify_data(content=get_css_file_data(self.event))


class RHLayoutCSSDelete(RHLayoutBase):
    def _process(self):
        self.event.stylesheet = None
        self.event.stylesheet_metadata = None
        layout_settings.set(self.event, 'use_custom_css', False)
        flash(_('CSS file deleted'), 'success')
        logger.info('CSS file for %s deleted by %s', self.event, session.user)
        return jsonify_data(content=None)


class RHLayoutCSSPreview(RHLayoutBase):
    def _process(self):
        form = CSSSelectionForm(event=self.event, formdata=request.args, csrf_enabled=False)
        css_url = None
        js_url = None
        if form.validate():
            css_url = get_css_url(self.event, force_theme=form.theme.data, for_preview=True)
            js_url = get_js_url(self.event, force_theme=form.theme.data, for_preview=True)
        return WPConferenceDisplay(self, self.event, css_override_form=form, css_url_override=css_url,
                                   js_url_override=js_url).display()


class RHLayoutViewStylesheet(RHLayoutBase):
    def _process(self):
        form = CSSSelectionForm(event=self.event, formdata=request.args, csrf_enabled=False)
        if not form.validate() or not form.theme.data:
            raise NotFound
        return redirect(get_css_url(self.event, force_theme=form.theme.data))


class RHLayoutCSSSaveTheme(RHLayoutBase):
    def _process(self):
        form = CSSSelectionForm(event=self.event)
        if form.validate_on_submit():
            layout_settings.set(self.event, 'use_custom_css', form.theme.data == '_custom')
            if form.theme.data != '_custom':
                layout_settings.set(self.event, 'theme', form.theme.data)
            flash(_('Settings saved'), 'success')
            return redirect(url_for('.index', self.event))


class RHLogoDisplay(RHDisplayEventBase):
    def _check_access(self):
        if (
            (token := request.args.get('token')) and
            secure_serializer.loads(token, salt='event-logo-download') == self.event.id
        ):
            return

        try:
            RHDisplayEventBase._check_access(self)
        except RegistrationRequired:
            # in case registrants need to register they should be able to see the logo
            pass

    def _process(self):
        if not self.event.has_logo:
            raise NotFound
        metadata = self.event.logo_metadata
        return send_file(metadata['filename'], BytesIO(self.event.logo), mimetype=metadata['content_type'],
                         conditional=True, no_cache=False)


class RHLayoutCSSDisplay(RHDisplayEventBase):
    def _check_access(self):
        try:
            RHDisplayEventBase._check_access(self)
        except RegistrationRequired:
            # in case registrants need to register they should be able to see the css
            pass

    def _process(self):
        if not self.event.has_stylesheet:
            raise NotFound
        data = BytesIO(self.event.stylesheet.encode())
        return send_file(self.event.stylesheet_metadata['filename'], data, mimetype='text/css', conditional=True,
                         no_cache=False)
