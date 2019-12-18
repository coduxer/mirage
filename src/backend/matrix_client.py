import asyncio
import html
import io
import logging as log
import platform
import re
import traceback
from contextlib import suppress
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import (
    Any, DefaultDict, Dict, List, NamedTuple, Optional, Set, Tuple, Type,
    Union,
)
from urllib.parse import urlparse
from uuid import UUID, uuid4

import cairosvg
from PIL import Image as PILImage
from pymediainfo import MediaInfo

import nio
from nio.crypto import AsyncDataT as UploadData
from nio.crypto import async_generator_from_data

from .__about__ import __pkg_name__, __pretty_name__
from . import utils
from .errors import (
    BadMimeType, InvalidUserId, InvalidUserInContext, MatrixError,
    UneededThumbnail, UserNotFound,
)
from .html_filter import HTML_FILTER
from .models.items import (
    Account, Event, Member, Room, TypeSpecifier, Upload, UploadStatus,
)
from .models.model_store import ModelStore
from .pyotherside_events import AlertRequested

CryptDict = Dict[str, Any]


class UploadReturn(NamedTuple):
    mxc:             str
    mime:            str
    decryption_dict: Dict[str, Any]


class MatrixImageInfo(NamedTuple):
    w:        int
    h:        int
    mimetype: str
    size:     int


class MatrixClient(nio.AsyncClient):
    user_id_regex          = re.compile(r"^@.+:.+")
    room_id_or_alias_regex = re.compile(r"^[#!].+:.+")
    http_s_url             = re.compile(r"^https?://")

    def __init__(self,
                 backend,
                 user:       str,
                 homeserver: str           = "https://matrix.org",
                 device_id:  Optional[str] = None) -> None:

        if not urlparse(homeserver).scheme:
            raise ValueError(
                f"homeserver is missing scheme (e.g. https://): {homeserver}",
            )

        store = Path(backend.app.appdirs.user_data_dir) / "encryption"
        store.mkdir(parents=True, exist_ok=True)

        super().__init__(
            homeserver = homeserver,
            user       = user,
            device_id  = device_id,
            store_path = store,
            config     = nio.AsyncClientConfig(
                max_timeout_retry_wait_time = 10,
                # TODO: pass a custom encryption DB pickle key?
            ),
        )

        from .backend import Backend
        self.backend: Backend    = backend
        self.models:  ModelStore = self.backend.models

        self.profile_task:    Optional[asyncio.Future] = None
        self.sync_task:       Optional[asyncio.Future] = None
        self.load_rooms_task: Optional[asyncio.Future] = None
        self.first_sync_done: asyncio.Event            = asyncio.Event()
        self.first_sync_date: Optional[datetime]       = None

        self.past_tokens:          Dict[str, str] = {}     # {room_id: token}
        self.fully_loaded_rooms:   Set[str]       = set()  # {room_id}
        self.loaded_once_rooms:    Set[str]       = set()  # {room_id}
        self.cleared_events_rooms: Set[str]       = set()  # {room_id}

        self.skipped_events: DefaultDict[str, int] = DefaultDict(lambda: 0)

        from .nio_callbacks import NioCallbacks
        self.nio_callbacks = NioCallbacks(self)


    def __repr__(self) -> str:
        return "%s(user_id=%r, homeserver=%r, device_id=%r)" % (
            type(self).__name__, self.user_id, self.homeserver, self.device_id,
        )


    @property
    def default_device_name(self) -> str:
        os_ = f" on {platform.system()}".rstrip()
        os_ = f"{os_} {platform.release()}".rstrip() if os_ != " on" else ""
        return f"{__pretty_name__}{os_}"


    async def login(self, password: str, device_name: str = "") -> None:
        response = await super().login(
            password, device_name or self.default_device_name,
        )

        if isinstance(response, nio.LoginError):
            raise MatrixError.from_nio(response)

        asyncio.ensure_future(self.start())


    async def resume(self, user_id: str, token: str, device_id: str) -> None:
        response = nio.LoginResponse(user_id, device_id, token)
        await self.receive_response(response)

        asyncio.ensure_future(self.start())


    async def logout(self) -> None:
        for task in (self.profile_task, self.load_rooms_task, self.sync_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        await super().logout()
        await self.close()


    @property
    def syncing(self) -> bool:
        if not self.sync_task:
            return False

        return not self.sync_task.done()


    async def start(self) -> None:
        def on_profile_response(future) -> None:
            exception = future.exception()

            if exception:
                log.warn("On %s client startup: %r", self.user_id, exception)
                self.profile_task = asyncio.ensure_future(
                    self.backend.get_profile(self.user_id),
                )
                self.profile_task.add_done_callback(on_profile_response)
                return

            resp                    = future.result()
            account                 = self.models[Account][self.user_id]
            account.profile_updated = datetime.now()
            account.display_name    = resp.displayname or ""
            account.avatar_url      = resp.avatar_url or ""

        self.profile_task = asyncio.ensure_future(
            self.backend.get_profile(self.user_id),
        )
        self.profile_task.add_done_callback(on_profile_response)

        while True:
            try:
                self.sync_task = asyncio.ensure_future(
                    self.sync_forever(timeout=10_000),
                )
                await self.sync_task
                break  # task cancelled
            except Exception:
                trace = traceback.format_exc().rstrip()
                log.error("Exception during sync, will restart:\n%s", trace)
                await asyncio.sleep(2)


    @property
    def all_rooms(self) -> Dict[str, nio.MatrixRoom]:
        return {**self.invited_rooms, **self.rooms}


    async def send_text(self, room_id: str, text: str) -> None:
        escape = False
        if text.startswith("//") or text.startswith(r"\/"):
            escape = True
            text   = text[1:]

        if text.startswith("/me ") and not escape:
            event_type = nio.RoomMessageEmote
            text       = text[len("/me "): ]
            content    = {"body": text, "msgtype": "m.emote"}
            to_html    = HTML_FILTER.from_markdown_inline(text, outgoing=True)
            echo_body  = HTML_FILTER.from_markdown_inline(text)
        else:
            event_type = nio.RoomMessageText
            content    = {"body": text, "msgtype": "m.text"}
            to_html    = HTML_FILTER.from_markdown(text, outgoing=True)
            echo_body  = HTML_FILTER.from_markdown(text)

        if to_html not in (html.escape(text), f"<p>{html.escape(text)}</p>"):
            content["format"]         = "org.matrix.custom.html"
            content["formatted_body"] = to_html

        # Can't use the standard Matrix transaction IDs; they're only visible
        # to the sender so our other accounts wouldn't be able to replace
        # local echoes by real messages.
        tx_id = uuid4()
        content[f"{__pkg_name__}.transaction_id"] = str(tx_id)

        await self._local_echo(room_id, tx_id, event_type, content=echo_body)
        await self._send_message(room_id, content)


    async def send_file(self, room_id: str, path: Union[Path, str]) -> None:
        item_uuid = uuid4()

        try:
            await self._send_file(item_uuid, room_id, path)
        except (nio.TransferCancelledError, asyncio.CancelledError):
            log.info("Deleting item for cancelled upload %s", item_uuid)
            del self.models[Upload, room_id][str(item_uuid)]


    async def _send_file(
        self, item_uuid: UUID, room_id: str, path: Union[Path, str],
    ) -> None:
        from .media_cache import Media, Thumbnail

        transaction_id = uuid4()
        path           = Path(path)
        encrypt        = room_id in self.encrypted_rooms

        try:
            size = path.resolve().stat().st_size
        except (PermissionError, FileNotFoundError):
            # This error will be caught again by the try block later below
            size = 0

        task        = asyncio.Task.current_task()
        monitor     = nio.TransferMonitor(size)
        upload_item = Upload(item_uuid, task, monitor, path, total_size=size)
        self.models[Upload, room_id][str(item_uuid)] = upload_item

        def on_transferred(transferred: int) -> None:
            upload_item.uploaded  = transferred

        def on_speed_changed(speed: float) -> None:
            upload_item.speed     = speed
            upload_item.time_left = monitor.remaining_time

        monitor.on_transferred   = on_transferred
        monitor.on_speed_changed = on_speed_changed

        try:
            url, mime, crypt_dict = await self.upload(
                lambda *_: path,
                filename = path.name,
                encrypt  = encrypt, monitor=monitor,
            )
        except (MatrixError, OSError) as err:
            upload_item.status     = UploadStatus.Error
            upload_item.error      = type(err)
            upload_item.error_args = err.args

            # Wait for cancellation from UI, see parent send_file() method
            while True:
                await asyncio.sleep(0.1)

        upload_item.status = UploadStatus.Caching
        await Media.from_existing_file(self.backend.media_cache, url, path)

        kind = (mime or "").split("/")[0]

        thumb_url:  str                       = ""
        thumb_info: Optional[MatrixImageInfo] = None

        content: dict = {
            f"{__pkg_name__}.transaction_id": str(transaction_id),

            "body": path.name,
            "info": {
                "mimetype": mime,
                "size":     upload_item.total_size,
            },
        }

        if encrypt:
            content["file"] = {"url": url, **crypt_dict}
        else:
            content["url"] = url

        if kind == "image":
            is_svg = mime == "image/svg+xml"

            event_type = \
                nio.RoomEncryptedImage if encrypt else nio.RoomMessageImage

            content["msgtype"] = "m.image"

            content["info"]["w"], content["info"]["h"] = (
                await utils.svg_dimensions(path) if is_svg else
                PILImage.open(path).size
            )

            try:
                thumb_data, thumb_info = await self.generate_thumbnail(
                    path, is_svg=is_svg,
                )
            except UneededThumbnail:
                pass
            except OSError as err:
                log.warning(f"Failed thumbnailing {path}: {err}")
            else:
                thumb_name = f"{path.stem}_thumbnail{''.join(path.suffixes)}"

                upload_item.status     = UploadStatus.Uploading
                upload_item.filepath   = Path(thumb_name)
                upload_item.total_size = len(thumb_data)

                try:
                    thumb_url, _, thumb_crypt_dict = await self.upload(
                        lambda *_: thumb_data,
                        filename =
                            f"{path.stem}_sample{''.join(path.suffixes)}",
                        encrypt  = encrypt,
                    )
                except MatrixError as err:
                    log.warning(f"Failed uploading thumbnail {path}: {err}")
                else:
                    upload_item.status = UploadStatus.Caching

                    await Thumbnail.from_bytes(
                        self.backend.media_cache,
                        thumb_url,
                        thumb_data,
                        wanted_size = (content["info"]["w"],
                                       content["info"]["h"]),
                    )

                    if encrypt:
                        content["info"]["thumbnail_file"]  = {
                            "url": thumb_url,
                            **thumb_crypt_dict,
                        }
                    else:
                        content["info"]["thumbnail_url"]  = thumb_url

                    content["info"]["thumbnail_info"] = thumb_info._asdict()

        elif kind == "audio":
            event_type = \
                nio.RoomEncryptedAudio if encrypt else nio.RoomMessageAudio

            content["msgtype"]          = "m.audio"
            content["info"]["duration"] = getattr(
                MediaInfo.parse(path).tracks[0], "duration", 0,
            ) or 0

        elif kind == "video":
            event_type = \
                nio.RoomEncryptedVideo if encrypt else nio.RoomMessageVideo

            content["msgtype"] = "m.video"

            tracks = MediaInfo.parse(path).tracks

            content["info"]["duration"] = \
                getattr(tracks[0], "duration", 0) or 0

            content["info"]["w"] = max(
                getattr(t, "width", 0) or 0 for t in tracks
            )
            content["info"]["h"] = max(
                getattr(t, "height", 0) or 0 for t in tracks
            )

        else:
            event_type = \
                nio.RoomEncryptedFile if encrypt else nio.RoomMessageFile

            content["msgtype"]  = "m.file"
            content["filename"] = path.name

        del self.models[Upload, room_id][str(upload_item.uuid)]

        await self._local_echo(
            room_id,
            transaction_id,
            event_type,
            inline_content   = path.name,
            media_url        = url,
            media_title      = path.name,
            media_width      = content["info"].get("w", 0),
            media_height     = content["info"].get("h", 0),
            media_duration   = content["info"].get("duration", 0),
            media_size       = content["info"]["size"],
            media_mime       = content["info"]["mimetype"],
            thumbnail_url    = thumb_url,
            thumbnail_width  =
                content["info"].get("thumbnail_info", {}).get("w", 0),
            thumbnail_height =
                content["info"].get("thumbnail_info", {}).get("h", 0),
        )

        await self._send_message(room_id, content)


    async def _local_echo(
        self, room_id: str, transaction_id: UUID,
        event_type: Type[nio.Event], **event_fields,
    ) -> None:

        our_info = self.models[Member, self.user_id, room_id][self.user_id]

        event = Event(
            source           = None,
            client_id        = f"echo-{transaction_id}",
            event_id         = "",
            date             = datetime.now(),
            sender_id        = self.user_id,
            sender_name      = our_info.display_name,
            sender_avatar    = our_info.avatar_url,
            is_local_echo    = True,
            local_event_type = event_type,
            **event_fields,
        )

        for user_id in self.models[Account]:
            if user_id in self.models[Member, self.user_id, room_id]:
                key = f"echo-{transaction_id}"
                self.models[Event, user_id, room_id][key] = event

                if user_id == self.user_id:
                    self.models[Event, user_id, room_id].sync_now()

        await self.set_room_last_event(room_id, event)


    async def _send_message(self, room_id: str, content: dict) -> None:

        async with self.backend.send_locks[room_id]:
            response = await self.room_send(
                room_id                   = room_id,
                message_type              = "m.room.message",
                content                   = content,
                ignore_unverified_devices = True,
            )

        if isinstance(response, nio.RoomSendError):
            raise MatrixError.from_nio(response)


    async def load_past_events(self, room_id: str) -> bool:
        if room_id in self.fully_loaded_rooms or \
           room_id in self.invited_rooms or \
           room_id in self.cleared_events_rooms:
            return False

        await self.first_sync_done.wait()

        while not self.past_tokens.get(room_id):
            # If a new room was added, wait for onSyncResponse to set the token
            await asyncio.sleep(0.1)

        response = await self.room_messages(
            room_id = room_id,
            start   = self.past_tokens[room_id],
            limit   = 100 if room_id in self.loaded_once_rooms else 25,
        )

        if isinstance(response, nio.RoomMessagesError):
            log.error("Loading past messages for room %s failed: %s",
                      room_id, response)
            return True

        self.loaded_once_rooms.add(room_id)
        more_to_load = True

        self.past_tokens[room_id] = response.end

        for event in response.chunk:
            if isinstance(event, nio.RoomCreateEvent):
                self.fully_loaded_rooms.add(room_id)
                more_to_load = False

            for cb in self.event_callbacks:
                if (cb.filter is None or isinstance(event, cb.filter)):
                    await cb.func(self.all_rooms[room_id], event)

        return more_to_load


    async def load_rooms_without_visible_events(self) -> None:
        for room_id in self.models[Room, self.user_id]:
            asyncio.ensure_future(
                self._load_room_without_visible_events(room_id),
            )


    async def _load_room_without_visible_events(self, room_id: str) -> None:
        events = self.models[Event, self.user_id, room_id]
        more   = True

        while self.skipped_events[room_id] and not events and more:
            more = await self.load_past_events(room_id)


    async def new_direct_chat(self, invite: str, encrypt: bool = False) -> str:
        if invite == self.user_id:
            raise InvalidUserInContext(invite)

        if not self.user_id_regex.match(invite):
            raise InvalidUserId(invite)

        if isinstance(await self.get_profile(invite), nio.ProfileGetError):
            raise UserNotFound(invite)

        response = await super().room_create(
            invite        = [invite],
            is_direct     = True,
            visibility    = nio.RoomVisibility.private,
            initial_state =
                [nio.EnableEncryptionBuilder().as_dict()] if encrypt else [],
        )

        if isinstance(response, nio.RoomCreateError):
            raise MatrixError.from_nio(response)

        return response.room_id


    async def new_group_chat(
        self,
        name:     Optional[str] = None,
        topic:    Optional[str] = None,
        public:   bool          = False,
        encrypt:  bool          = False,
        federate: bool          = True,
    ) -> str:

        response = await super().room_create(
            name       = name or None,
            topic      = topic or None,
            federate   = federate,
            visibility =
                nio.RoomVisibility.public if public else
                nio.RoomVisibility.private,
            initial_state =
                [nio.EnableEncryptionBuilder().as_dict()] if encrypt else [],
        )

        if isinstance(response, nio.RoomCreateError):
            raise MatrixError.from_nio(response)

        return response.room_id

    async def room_join(self, alias_or_id_or_url: str) -> str:
        string = alias_or_id_or_url.strip()

        if self.http_s_url.match(string):
            for part in urlparse(string).fragment.split("/"):
                if self.room_id_or_alias_regex.match(part):
                    string = part
                    break
            else:
                raise ValueError(f"No alias or room id found in url {string}")

        if not self.room_id_or_alias_regex.match(string):
            raise ValueError("Not an alias or room id")

        response = await super().join(string)

        if isinstance(response, nio.JoinError):
            raise MatrixError.from_nio(response)

        return response.room_id


    async def room_forget(self, room_id: str) -> None:
        await super().room_leave(room_id)
        await super().room_forget(room_id)
        self.models[Room, self.user_id].pop(room_id, None)
        self.models.pop((Event, self.user_id, room_id), None)
        self.models.pop((Member, self.user_id, room_id), None)


    async def room_mass_invite(
        self, room_id: str, *user_ids: str,
    ) -> Tuple[List[str], List[Tuple[str, Exception]]]:

        user_ids = tuple(
            uid for uid in user_ids
            # Server would return a 403 forbidden for users already in the room
            if uid not in self.all_rooms[room_id].users
        )

        async def invite(user):
            if not self.user_id_regex.match(user):
                return InvalidUserId(user)

            if isinstance(await self.get_profile(user), nio.ProfileGetError):
                return UserNotFound(user)

            return await self.room_invite(room_id, user)

        coros        = [invite(uid) for uid in user_ids]
        successes    = []
        errors: list = []
        responses    = await asyncio.gather(*coros)

        for user_id, response in zip(user_ids, responses):
            if isinstance(response, nio.RoomInviteError):
                errors.append((user_id, MatrixError.from_nio(response)))

            elif isinstance(response, Exception):
                errors.append((user_id, response))

            else:
                successes.append(user_id)

        return (successes, errors)


    async def generate_thumbnail(
        self, data: UploadData, is_svg: bool = False,
    ) -> Tuple[bytes, MatrixImageInfo]:

        png_modes = ("1", "L", "P", "RGBA")

        data   = b"".join([c async for c in async_generator_from_data(data)])
        is_svg = await utils.guess_mime(data) == "image/svg+xml"

        if is_svg:
            svg_width, svg_height = await utils.svg_dimensions(data)

            data = cairosvg.svg2png(
                bytestring    = data,
                parent_width  = svg_width,
                parent_height = svg_height,
            )

        thumb = PILImage.open(io.BytesIO(data))

        small       = thumb.width <= 800 and thumb.height <= 600
        is_jpg_png  = thumb.format in ("JPEG", "PNG")
        jpgable_png = thumb.format == "PNG" and thumb.mode not in png_modes

        if small and is_jpg_png and not jpgable_png and not is_svg:
            raise UneededThumbnail()

        if not small:
            thumb.thumbnail((800, 600), PILImage.LANCZOS)

        with io.BytesIO() as out:
            if thumb.mode in png_modes:
                thumb.save(out, "PNG", optimize=True)
                mime = "image/png"
            else:
                thumb.convert("RGB").save(out, "JPEG", optimize=True)
                mime = "image/jpeg"

            data = out.getvalue()

        info = MatrixImageInfo(thumb.width, thumb.height, mime, len(data))
        return (data, info)


    async def upload(
        self,
        data_provider: nio.DataProvider,
        mime:          Optional[str]                 = None,
        filename:      Optional[str]                 = None,
        encrypt:       bool                          = False,
        monitor:       Optional[nio.TransferMonitor] = None,
    ) -> UploadReturn:

        mime = mime or await utils.guess_mime(data_provider(0, 0))

        response, decryption_dict = await super().upload(
            data_provider,
            "application/octet-stream" if encrypt else mime,
            filename,
            encrypt,
            monitor,
        )

        if isinstance(response, nio.UploadError):
            raise MatrixError.from_nio(response)

        return UploadReturn(response.content_uri, mime, decryption_dict)


    async def set_avatar_from_file(self, path: Union[Path, str]) -> None:
        mime = await utils.guess_mime(path)

        if mime.split("/")[0] != "image":
            raise BadMimeType(wanted="image/*", got=mime)

        mxc, *_ = await self.upload(lambda *_: path, mime, Path(path).name)
        await self.set_avatar(mxc)


    async def import_keys(self, infile: str, passphrase: str) -> None:
        await super().import_keys(infile, passphrase)
        await self.retry_decrypting_events()


    async def export_keys(self, outfile: str, passphrase: str) -> None:
        path = Path(outfile)
        path.parent.mkdir(parents=True, exist_ok=True)

        # The QML dialog asks the user if he wants to overwrite before this
        if path.exists():
            path.unlink()

        await super().export_keys(outfile, passphrase)


    async def retry_decrypting_events(self) -> None:
        for sync_id, model in self.models.items():
            if not (isinstance(sync_id, tuple) and
                    sync_id[0:2] == (Event, self.user_id)):
                continue

            _, _, room_id = sync_id

            for ev in model.values():
                room = self.all_rooms[room_id]

                if isinstance(ev.source, nio.MegolmEvent):
                    try:
                        decrypted = self.decrypt_event(ev.source)

                        if not decrypted:
                            raise nio.EncryptionError()

                    except nio.EncryptionError:
                        continue

                    for cb in self.event_callbacks:
                        if not cb.filter or isinstance(decrypted, cb.filter):
                            await asyncio.coroutine(cb.func)(room, decrypted)


    async def clear_events(self, room_id: str) -> None:
        self.cleared_events_rooms.add(room_id)
        model = self.models[Event, self.user_id, room_id]
        if model:
            model.clear()
            model.sync_now()


    # Functions to register data into models

    async def event_is_past(self, ev: Union[nio.Event, Event]) -> bool:
        if not self.first_sync_date:
            return True

        if isinstance(ev, Event):
            return ev.date < self.first_sync_date

        date = datetime.fromtimestamp(ev.server_timestamp / 1000)
        return date < self.first_sync_date


    async def set_room_last_event(self, room_id: str, item: Event) -> None:
        model = self.models[Room, self.user_id]
        room  = model[room_id]

        if room.last_event is None:
            room.last_event = item.serialized

            if item.is_local_echo:
                model.sync_now()

            return

        is_profile_ev = item.type_specifier == TypeSpecifier.profile_change

        # If there were no better events available to show previously
        prev_is_profile_ev = \
            room.last_event["type_specifier"] == TypeSpecifier.profile_change

        # If this is a profile event, only replace the currently shown one if
        # it was also a profile event (we had nothing better to show).
        if is_profile_ev and not prev_is_profile_ev:
            return

        # If this event is older than the currently shown one, only replace
        # it if the previous was a profile event.
        if item.date < room.last_event["date"] and not prev_is_profile_ev:
            return

        room.last_event = item.serialized

        if item.is_local_echo:
            model.sync_now()


    async def register_nio_room(self, room: nio.MatrixRoom, left: bool = False,
                               ) -> None:
        # Add room
        try:
            last_ev = self.models[Room, self.user_id][room.room_id].last_event
        except KeyError:
            last_ev = None

        inviter        = getattr(room, "inviter", "") or ""
        levels         = room.power_levels
        can_send_state = partial(levels.can_user_send_state, self.user_id)
        can_send_msg   = partial(levels.can_user_send_message, self.user_id)

        self.models[Room, self.user_id][room.room_id] = Room(
            room_id        = room.room_id,
            given_name     = room.name or "",
            display_name   = room.display_name or "",
            avatar_url     = room.gen_avatar_url or "",
            plain_topic    = room.topic or "",
            topic          = HTML_FILTER.filter_inline(room.topic or ""),
            inviter_id     = inviter,
            inviter_name   = room.user_name(inviter) if inviter else "",
            inviter_avatar =
                (room.avatar_url(inviter) or "") if inviter else "",
            left           = left,

            encrypted       = room.encrypted,
            invite_required = room.join_rule == "invite",
            guests_allowed  = room.guest_access == "can_join",

            can_invite           = levels.can_user_invite(self.user),
            can_send_messages    = can_send_msg(),
            can_set_name         = can_send_state("m.room.name"),
            can_set_topic        = can_send_state("m.room.topic"),
            can_set_avatar       = can_send_state("m.room.avatar"),
            can_set_encryption   = can_send_state("m.room.encryption"),
            can_set_join_rules   = can_send_state("m.room.join_rules"),
            can_set_guest_access = can_send_state("m.room.guest_access"),

            last_event = last_ev,
        )

        # List members that left the room, then remove them from our model
        left_the_room = [
            user_id
            for user_id in self.models[Member, self.user_id, room.room_id]
            if user_id not in room.users
        ]

        for user_id in left_the_room:
            del self.models[Member, self.user_id, room.room_id][user_id]

        # Add the room members to the added room
        new_dict = {
            user_id: Member(
                user_id      = user_id,
                display_name = room.user_name(user_id)  # disambiguated
                               if member.display_name else "",
                avatar_url   = member.avatar_url or "",
                typing       = user_id in room.typing_users,
                power_level  = member.power_level,
                invited      = member.invited,
            ) for user_id, member in room.users.items()
        }
        self.models[Member, self.user_id, room.room_id].update(new_dict)


    async def get_member_name_avatar(self, room_id: str, user_id: str,
                                    ) -> Tuple[str, str]:
        try:
            item = self.models[Member, self.user_id, room_id][user_id]
        except KeyError:  # e.g. user is not anymore in the room
            try:
                info = await self.backend.get_profile(user_id)
                return (info.displayname or "", info.avatar_url or "")
            except MatrixError:
                return ("", "")
        else:
            return (item.display_name, item.avatar_url)


    async def register_nio_event(
        self, room: nio.MatrixRoom, ev: nio.Event, **fields,
    ) -> None:

        await self.register_nio_room(room)

        sender_name, sender_avatar = \
            await self.get_member_name_avatar(room.room_id, ev.sender)

        target_id = getattr(ev, "state_key", "") or ""

        target_name, target_avatar = \
            await self.get_member_name_avatar(room.room_id, target_id) \
            if target_id else ("", "")

        # Create Event ModelItem
        item = Event(
            source        = ev,
            client_id     = ev.event_id,
            event_id      = ev.event_id,
            date          = datetime.fromtimestamp(ev.server_timestamp / 1000),
            sender_id     = ev.sender,
            sender_name   = sender_name,
            sender_avatar = sender_avatar,
            target_id     = target_id,
            target_name   = target_name,
            target_avatar = target_avatar,
            **fields,
        )

        # Add the Event to model
        tx_id = ev.source.get("content", {}).get(
            f"{__pkg_name__}.transaction_id",
        )
        local_sender = ev.sender in self.backend.clients

        if local_sender and tx_id:
            item.client_id = f"echo-{tx_id}"

        if not local_sender and not await self.event_is_past(ev):
            AlertRequested()

        self.models[Event, self.user_id, room.room_id][item.client_id] = item

        await self.set_room_last_event(room.room_id, item)

        if item.sender_id == self.user_id:
            self.models[Event, self.user_id, room.room_id].sync_now()