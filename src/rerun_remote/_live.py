"""Relay one recording from a Rerun 0.43 message proxy into the owned viewer."""

import threading
import time

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.message import DecodeError

from .cli import ViewError

_SERVICE = "/rerun.sdk_comms.v1alpha1.MessageProxyService/"
_OPTIONS = (("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.enable_http_proxy", 0),
            ("grpc.enable_retries", 0))
_SELECTION_SECONDS = 15


def _response_type():
    # Only decode the store identity. Retain the original envelope, including opaque Arrow chunks and unknown fields.
    # https://github.com/rerun-io/rerun/tree/0.43.2/crates/store/re_protos/proto/rerun/v1alpha1
    schema = descriptor_pb2.FileDescriptorProto(name="rerun_remote_proxy", syntax="proto3")
    fields = {
        "ApplicationId": ((1, "id", "string"),),
        "StoreId": ((1, "kind", "uint32"), (2, "recording_id", "string"), (3, "application_id", "ApplicationId")),
        "StoreInfo": ((1, "application_id", "ApplicationId"), (2, "store_id", "StoreId")),
        "SetStoreInfo": ((2, "info", "StoreInfo"),),
        "ArrowMsg": ((1, "store_id", "StoreId"),),
        "BlueprintActivation": ((1, "blueprint_id", "StoreId"),),
        "LogMsg": ((1, "set_store_info", "SetStoreInfo"), (2, "arrow_msg", "ArrowMsg"),
                   (3, "blueprint_activation", "BlueprintActivation")),
        "ReadMessagesResponse": ((1, "log_msg", "LogMsg"),),
    }
    descriptor = descriptor_pb2.FieldDescriptorProto
    scalars = {"string": descriptor.TYPE_STRING, "uint32": descriptor.TYPE_UINT32}
    for name, members in fields.items():
        message = schema.message_type.add(name=name)
        for number, field_name, type_name in members:
            field = message.field.add(name=field_name, number=number, type=scalars.get(type_name, descriptor.TYPE_MESSAGE))
            if type_name not in scalars:
                field.type_name = "." + type_name
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("ReadMessagesResponse"))


_Response = _response_type()


class _Selection:
    def __init__(self, identity: tuple[str, str]):
        self.application, self.recording = identity
        self.metadata = False

    def includes(self, envelope: bytes) -> tuple[bool, bool]:
        message = _Response.FromString(envelope).log_msg
        variants = message.ListFields()
        if len(variants) != 1:
            raise ViewError("The upstream Rerun proxy sent an unsupported log envelope.")
        field, body = variants[0]
        if field.name == "set_store_info":
            store = body.info.store_id
        elif field.name == "arrow_msg":
            store = body.store_id
        else:
            store = body.blueprint_id
        if store.application_id.id != self.application or not store.recording_id or store.kind not in (1, 2):
            return False, False
        if store.kind == 1 and store.recording_id != self.recording:
            return False, False
        if field.name == "set_store_info" and body.info.application_id.id not in ("", self.application):
            raise ViewError("The selected Rerun store has conflicting application identities.")
        if store.kind == 2:
            return True, False  # Blueprint layout is scoped to the application in Rerun.
        if field.name == "set_store_info":
            self.metadata = True
            return True, False
        data = self.metadata and field.name == "arrow_msg"
        return data, data


class SelectedLiveRelay:
    def __init__(self, upstream: str, viewer: str, identity: tuple[str, str]):
        self.identity = identity
        self.deadline = time.monotonic() + _SELECTION_SECONDS
        self.data = threading.Event()
        self.failure = None
        self.source = grpc.insecure_channel(upstream, options=_OPTIONS)
        self.destination = grpc.insecure_channel(viewer, options=_OPTIONS)
        try:
            self.reader = self.source.unary_stream(_SERVICE + "ReadMessages")(b"")
            self.writer = self.destination.stream_unary(_SERVICE + "WriteMessages").future(self._messages())
        except BaseException:
            self.close()
            raise

    def _messages(self):
        selection = _Selection(self.identity)
        try:
            for envelope in self.reader:
                include, data = selection.includes(envelope)
                if data:
                    self.data.set()
                if include:
                    # ReadMessagesResponse and WriteMessagesRequest both use field 1 for the same LogMsg.
                    yield envelope
            self.failure = "The upstream live Rerun stream ended."
        except grpc.RpcError as error:
            self.failure = f"The upstream live Rerun stream failed ({error.code().name})."
        except DecodeError:
            self.failure = "The upstream live Rerun stream sent malformed recording metadata."
        except ViewError as error:
            self.failure = str(error)

    def check(self) -> None:
        if self.failure:
            raise ViewError(self.failure)
        if self.writer.done():
            error = self.writer.exception()
            code = f" ({error.code().name})" if isinstance(error, grpc.RpcError) else ""
            raise ViewError(f"The native viewer stopped receiving the selected recording{code}.")
        if not self.data.is_set() and time.monotonic() >= self.deadline:
            application, recording = self.identity
            raise ViewError(f"No data for Rerun application {application!r}, recording {recording!r} within {_SELECTION_SECONDS} seconds. Check both IDs and that the producer supplies complete Rerun store identities.")

    def close(self) -> None:
        if hasattr(self, "writer"):
            self.writer.cancel()
        self.destination.close()
        self.source.close()
