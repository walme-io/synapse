# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module controls the reliability for application service transactions.

The nominal flow through this module looks like:
             _________
---ASa[e]-->|  Event  |
----ASb[e]->| Grouper |<-poll 1/s--+
--ASa[e]--->|_________|            | ASa[e,e]  ASb[e]
                                   V
      -````````-            +------------+
      |````````|<--StoreTxn-|Transaction |
      |Database|            | Controller |---> SEND TO AS
      `--------`            +------------+
What happens on SEND TO AS depends on the state of the Application Service:
 - If the AS is marked as DOWN, do nothing.
 - If the AS is marked as UP, send the transaction.
     * SUCCESS : Increment where the AS is up to txn-wise and nuke the txn
                 contents from the db.
     * FAILURE : Marked AS as DOWN and start Recoverer.

Recoverer attempts to recover ASes who have died. The flow for this looks like:
                ,--------------------- backoff++ --------------.
               V                                               |
  START ---> Wait exp ------> Get oldest txn ID from ----> FAILURE
             backoff           DB and try to send it
                                 ^                |___________
Mark AS as                       |                            V
UP & quit           +---------- YES                       SUCCESS
    |               |                                         |
    NO <--- Have more txns? <------ Mark txn success & nuke <-+
                                      from db; incr AS pos.
                                         Reset backoff.

This is all tied together by the AppServiceScheduler which DIs the required
components.
"""

from twisted.internet import defer


class AppServiceScheduler(object):
    """ Public facing API for this module. Does the required DI to tie the
    components together. This also serves as the "event_pool", which in this
    case is a simple array.
    """

    def __init__(self, clock, store, as_api):
        self.clock = clock
        self.store = store
        self.as_api = as_api
        self.event_grouper = _EventGrouper()

        def create_recoverer(service, callback):
            return _Recoverer(clock, store, as_api, service, callback)

        self.txn_ctrl = _TransactionController(
            clock, store, as_api, self.event_grouper, create_recoverer
        )

    @defer.inlineCallbacks
    def start(self):
        # check for any DOWN ASes and start recoverers for them.
        recoverers = yield _Recoverer.start(
            self.clock, self.store, self.as_api, self.txn_ctrl.on_recovered
        )
        self.txn_ctrl.add_recoverers(recoverers)
        self.txn_ctrl.start_polling()

    def submit_event_for_as(self, service, event):
        self.event_grouper.on_receive(service, event)


class AppServiceTransaction(object):
    """Represents an application service transaction."""

    def __init__(self, service, id, events):
        self.service = service
        self.id = id
        self.events = events

    def send(self, as_api):
        """Sends this transaction using the provided AS API interface.

        Args:
            as_api(ApplicationServiceApi): The API to use to send.
        Returns:
            A Deferred which resolves to True if the transaction was sent.
        """
        return as_api.push_bulk(
            service=self.service,
            events=self.events,
            txn_id=self.id
        )

    def complete(self, store):
        """Completes this transaction as successful.

        Marks this transaction ID on the application service and removes the
        transaction contents from the database.

        Args:
            store: The database store to operate on.
        Returns:
            A Deferred which resolves to True if the transaction was completed.
        """
        return store.complete_appservice_txn(
            service=self.service,
            txn_id=self.id
        )


class _EventGrouper(object):
    """Groups events for the same application service together.
    """

    def __init__(self):
        self.groups = {}  # dict of {service: [events]}

    def on_receive(self, service, event):
        if service not in self.groups:
            self.groups[service] = []
        self.groups[service].append(event)

    def drain_groups(self):
        groups = self.groups
        self.groups = {}
        return groups


class _TransactionController(object):

    def __init__(self, clock, store, as_api, event_grouper, recoverer_fn):
        self.clock = clock
        self.store = store
        self.as_api = as_api
        self.event_grouper = event_grouper
        self.recoverer_fn = recoverer_fn
        # keep track of how many recoverers there are
        self.recoverers = []

    def start_polling(self):
        groups = self.event_grouper.drain_groups()
        for service in groups:
            txn_id = self._get_next_txn_id(service)
            txn = AppServiceTransaction(service, txn_id, groups[service])
            self._store_txn(txn)
            if self._is_service_up(service):
                if txn.send(self.as_api):
                    txn.complete(self.store)
                else:
                    # TODO mark AS as down
                    self._start_recoverer(service)
        self.clock.call_later(1000, self.start_polling)

    def on_recovered(self, service):
        # TODO mark AS as UP
        pass

    def add_recoverers(self, recoverers):
        for r in recoverers:
            self.recoverers.append(r)

    def _start_recoverer(self, service):
        recoverer = self.recoverer_fn(service, self.on_recovered)
        recoverer.recover()

    def _is_service_up(self, service):
        pass

    def _get_next_txn_id(self, service):
        pass  # TODO work out the next txn_id for this service

    def _store_txn(self, txn):
        pass


class _Recoverer(object):

    @staticmethod
    @defer.inlineCallbacks
    def start(clock, store, as_api, callback):
        services = yield store.get_failing_appservices()
        recoverers = [
            _Recoverer(clock, store, as_api, s, callback) for s in services
        ]
        for r in recoverers:
            r.recover()
        defer.returnValue(recoverers)

    def __init__(self, clock, store, as_api, service, callback):
        self.clock = clock
        self.store = store
        self.as_api = as_api
        self.service = service
        self.callback = callback
        self.backoff_counter = 1

    def recover(self):
        self.clock.call_later(1000 * (2 ** self.backoff_counter), self.retry)

    @defer.inlineCallbacks
    def retry(self):
        txn = yield self._get_oldest_txn()
        if txn:
            if txn.send(self.as_api):
                txn.complete(self.store)
                # reset the backoff counter and retry immediately
                self.backoff_counter = 1
                yield self.retry()
            else:
                self.backoff_counter += 1
                self.recover()
        else:
            self._set_service_recovered()

    def _set_service_recovered(self):
        self.callback(self.service)

    @defer.inlineCallbacks
    def _get_oldest_txn(self):
        txn = yield self.store.get_oldest_txn(self.service)
        defer.returnValue(txn)
