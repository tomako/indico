# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

from ipaddress import ip_network
from operator import itemgetter

from indico.util.i18n import _
from indico.web.forms.fields import MultiStringField


class MultiIPNetworkField(MultiStringField):
    """A field to enter multiple IPv4 or IPv6 networks.

    The field data is a set of ``IPNetwork``s not bound to a DB session.
    The ``unique`` and ``sortable`` parameters of the parent class cannot be used with this class.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, field=('subnet', _('subnet')), **kwargs)
        self._data_converted = False
        self.data = None

    def _value(self):
        if self.data is None:
            return []
        elif self._data_converted:
            data = [{self.field_name: str(network)} for network in self.data or []]
            return sorted(data, key=itemgetter(self.field_name))
        else:
            return self.data

    def process_data(self, value):
        if value is not None:
            self._data_converted = True
            self.data = value

    def _fix_network(self, network):
        # XXX: not sure why we need the ascii dance here, but keeping it just in case
        network = network.encode('ascii', 'ignore').decode()
        # convert ipv6-style ipv4 to regular ipv4 since the ipaddress library doesn't deal with such IPs properly!
        return network.removeprefix('::ffff:')

    def process_formdata(self, valuelist):
        self._data_converted = False
        super().process_formdata(valuelist)
        self.data = {ip_network(self._fix_network(entry[self.field_name])) for entry in self.data}
        self._data_converted = True

    def pre_validate(self, form):
        pass  # nothing to do
