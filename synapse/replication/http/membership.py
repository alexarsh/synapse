# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
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

import logging
from typing import TYPE_CHECKING

from synapse.http.servlet import parse_json_object_from_request
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import Requester, UserID
from synapse.util.distributor import user_joined_room, user_left_room

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationRemoteJoinRestServlet(ReplicationEndpoint):
    """Does a remote join for the given user to the given room

    Request format:

        POST /_synapse/replication/remote_join/:room_id/:user_id

        {
            "requester": ...,
            "remote_room_hosts": [...],
            "content": { ... }
        }
    """

    NAME = "remote_join"
    PATH_ARGS = ("room_id", "user_id")

    def __init__(self, hs):
        super(ReplicationRemoteJoinRestServlet, self).__init__(hs)

        self.federation_handler = hs.get_handlers().federation_handler
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()

    @staticmethod
    def _serialize_payload(requester, room_id, user_id, remote_room_hosts, content):
        """
        Args:
            requester(Requester)
            room_id (str)
            user_id (str)
            remote_room_hosts (list[str]): Servers to try and join via
            content(dict): The event content to use for the join event
        """
        return {
            "requester": requester.serialize(),
            "remote_room_hosts": remote_room_hosts,
            "content": content,
        }

    async def _handle_request(self, request, room_id, user_id):
        content = parse_json_object_from_request(request)

        remote_room_hosts = content["remote_room_hosts"]
        event_content = content["content"]

        requester = Requester.deserialize(self.store, content["requester"])

        if requester.user:
            request.authenticated_entity = requester.user.to_string()

        logger.info("remote_join: %s into room: %s", user_id, room_id)

        event_id, stream_id = await self.federation_handler.do_invite_join(
            remote_room_hosts, room_id, user_id, event_content
        )

        return 200, {"event_id": event_id, "stream_id": stream_id}


class ReplicationRemoteRejectInviteRestServlet(ReplicationEndpoint):
    """Rejects the invite for the user and room.

    Request format:

        POST /_synapse/replication/remote_reject_invite/:room_id/:user_id

        {
            "requester": ...,
            "remote_room_hosts": [...],
            "content": { ... }
        }
    """

    NAME = "remote_reject_invite"
    PATH_ARGS = ("room_id", "user_id")

    def __init__(self, hs):
        super(ReplicationRemoteRejectInviteRestServlet, self).__init__(hs)

        self.federation_handler = hs.get_handlers().federation_handler
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        self.member_handler = hs.get_room_member_handler()

    @staticmethod
    def _serialize_payload(requester, room_id, user_id, remote_room_hosts, content):
        """
        Args:
            requester(Requester)
            room_id (str)
            user_id (str)
            remote_room_hosts (list[str]): Servers to try and reject via
        """
        return {
            "requester": requester.serialize(),
            "remote_room_hosts": remote_room_hosts,
            "content": content,
        }

    async def _handle_request(self, request, room_id, user_id):
        content = parse_json_object_from_request(request)

        remote_room_hosts = content["remote_room_hosts"]
        event_content = content["content"]

        requester = Requester.deserialize(self.store, content["requester"])

        if requester.user:
            request.authenticated_entity = requester.user.to_string()

        logger.info("remote_reject_invite: %s out of room: %s", user_id, room_id)

        try:
            event, stream_id = await self.federation_handler.do_remotely_reject_invite(
                remote_room_hosts, room_id, user_id, event_content,
            )
            event_id = event.event_id
        except Exception as e:
            # if we were unable to reject the exception, just mark
            # it as rejected on our end and plough ahead.
            #
            # The 'except' clause is very broad, but we need to
            # capture everything from DNS failures upwards
            #
            logger.warning("Failed to reject invite: %s", e)

            stream_id = await self.member_handler.locally_reject_invite(
                user_id, room_id
            )
            event_id = None

        return 200, {"event_id": event_id, "stream_id": stream_id}


class ReplicationLocallyRejectInviteRestServlet(ReplicationEndpoint):
    """Rejects the invite for the user and room locally.

    Request format:

        POST /_synapse/replication/locally_reject_invite/:room_id/:user_id

        {}
    """

    NAME = "locally_reject_invite"
    PATH_ARGS = ("room_id", "user_id")

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.member_handler = hs.get_room_member_handler()

    @staticmethod
    def _serialize_payload(room_id, user_id):
        return {}

    async def _handle_request(self, request, room_id, user_id):
        logger.info("locally_reject_invite: %s out of room: %s", user_id, room_id)

        stream_id = await self.member_handler.locally_reject_invite(user_id, room_id)

        return 200, {"stream_id": stream_id}


class ReplicationUserJoinedLeftRoomRestServlet(ReplicationEndpoint):
    """Notifies that a user has joined or left the room

    Request format:

        POST /_synapse/replication/membership_change/:room_id/:user_id/:change

        {}
    """

    NAME = "membership_change"
    PATH_ARGS = ("room_id", "user_id", "change")
    CACHE = False  # No point caching as should return instantly.

    def __init__(self, hs):
        super(ReplicationUserJoinedLeftRoomRestServlet, self).__init__(hs)

        self.registeration_handler = hs.get_registration_handler()
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        self.distributor = hs.get_distributor()

    @staticmethod
    def _serialize_payload(room_id, user_id, change):
        """
        Args:
            room_id (str)
            user_id (str)
            change (str): Either "joined" or "left"
        """
        assert change in ("joined", "left")

        return {}

    def _handle_request(self, request, room_id, user_id, change):
        logger.info("user membership change: %s in %s", user_id, room_id)

        user = UserID.from_string(user_id)

        if change == "joined":
            user_joined_room(self.distributor, user, room_id)
        elif change == "left":
            user_left_room(self.distributor, user, room_id)
        else:
            raise Exception("Unrecognized change: %r", change)

        return 200, {}


def register_servlets(hs, http_server):
    ReplicationRemoteJoinRestServlet(hs).register(http_server)
    ReplicationRemoteRejectInviteRestServlet(hs).register(http_server)
    ReplicationUserJoinedLeftRoomRestServlet(hs).register(http_server)
    ReplicationLocallyRejectInviteRestServlet(hs).register(http_server)
