"""Thin wrapper around pymqi so the server can be tested without IBM MQ.

The server talks to anything that implements MQClient; tests use an
in-memory fake, production uses PyMQIClient.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Message:
    body: bytes
    msg_id: str
    correl_id: str
    put_time: str


class MQClient(Protocol):
    def depth(self, queue: str) -> int: ...
    def browse(self, queue: str, limit: int) -> list[Message]: ...
    def put(self, queue: str, body: bytes, correl_id: str | None = None, trace_id: str | None = None) -> str: ...
    def get(self, queue: str) -> Message | None: ...


def _hex(raw: bytes) -> str:
    return raw.rstrip(b"\x00").hex()


class PyMQIClient:
    """Production client using IBM MQ client libraries via pymqi."""

    def __init__(self) -> None:
        import pymqi  # imported lazily: needs the IBM MQ client installed

        self.pymqi = pymqi
        self.CMQC = pymqi.CMQC
        self.qmgr_name = os.environ["MQ_QMGR"]
        self.channel = os.environ.get("MQ_CHANNEL", "DEV.APP.SVRCONN")
        self.conn_info = os.environ["MQ_CONN_INFO"]  # e.g. "localhost(1414)"
        self.user = os.environ.get("MQ_USER") or None
        self.password = os.environ.get("MQ_PASSWORD") or None
        self._qmgr = None

    def _connect(self):
        if self._qmgr is None:
            self._qmgr = self.pymqi.connect(
                self.qmgr_name, self.channel, self.conn_info, self.user, self.password
            )
        return self._qmgr

    def _no_msg(self, err) -> bool:
        return getattr(err, "reason", None) == self.CMQC.MQRC_NO_MSG_AVAILABLE

    def depth(self, queue: str) -> int:
        q = self.pymqi.Queue(self._connect(), queue, self.CMQC.MQOO_INQUIRE)
        try:
            return int(q.inquire(self.CMQC.MQIA_CURRENT_Q_DEPTH))
        finally:
            q.close()

    def browse(self, queue: str, limit: int) -> list[Message]:
        C = self.CMQC
        q = self.pymqi.Queue(self._connect(), queue, C.MQOO_BROWSE)
        out: list[Message] = []
        try:
            option = C.MQGMO_BROWSE_FIRST
            while len(out) < limit:
                md, gmo = self.pymqi.MD(), self.pymqi.GMO()
                gmo.Options = option | C.MQGMO_NO_WAIT | C.MQGMO_FAIL_IF_QUIESCING
                try:
                    body = q.get(None, md, gmo)
                except self.pymqi.MQMIError as err:
                    if self._no_msg(err):
                        break
                    raise
                out.append(self._to_message(body, md))
                option = C.MQGMO_BROWSE_NEXT
        finally:
            q.close()
        return out

    def put(self, queue: str, body: bytes, correl_id: str | None = None, trace_id: str | None = None) -> str:
        C = self.CMQC
        md = self.pymqi.MD()
        md.Format = C.MQFMT_STRING
        if correl_id:
            md.CorrelId = correl_id.encode("utf-8")[:24].ljust(24, b"\x00")
        elif trace_id:
            # 16-byte W3C trace ID in the 24-byte CorrelId; consumers read its first 32 hex characters.
            md.CorrelId = bytes.fromhex(trace_id).ljust(24, b"\x00")
        q = self.pymqi.Queue(self._connect(), queue, C.MQOO_OUTPUT)
        try:
            q.put(body, md)
        finally:
            q.close()
        return _hex(md.MsgId)

    def get(self, queue: str) -> Message | None:
        C = self.CMQC
        md, gmo = self.pymqi.MD(), self.pymqi.GMO()
        gmo.Options = C.MQGMO_NO_WAIT | C.MQGMO_FAIL_IF_QUIESCING
        q = self.pymqi.Queue(self._connect(), queue, C.MQOO_INPUT_AS_Q_DEF)
        try:
            body = q.get(None, md, gmo)
        except self.pymqi.MQMIError as err:
            if self._no_msg(err):
                return None
            raise
        finally:
            q.close()
        return self._to_message(body, md)

    @staticmethod
    def _to_message(body: bytes, md) -> Message:
        put_date = md.PutDate.decode(errors="ignore").strip("\x00 ")
        put_time = md.PutTime.decode(errors="ignore").strip("\x00 ")
        return Message(
            body=body,
            msg_id=_hex(md.MsgId),
            correl_id=_hex(md.CorrelId),
            put_time=f"{put_date} {put_time}".strip(),
        )


class InMemoryClient:
    """Fake queue manager for tests and demos (no IBM MQ needed)."""

    def __init__(self, queues: dict[str, list[bytes]] | None = None):
        self.queues: dict[str, list[Message]] = {}
        self._seq = 0
        for name, bodies in (queues or {}).items():
            for body in bodies:
                self.put(name, body)

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._seq:048x}"

    def depth(self, queue: str) -> int:
        return len(self.queues.get(queue, []))

    def browse(self, queue: str, limit: int) -> list[Message]:
        return list(self.queues.get(queue, [])[:limit])

    def put(self, queue: str, body: bytes, correl_id: str | None = None, trace_id: str | None = None) -> str:
        msg_id = self._next_id()
        correl = (correl_id or "").encode().hex() if correl_id or not trace_id else trace_id
        self.queues.setdefault(queue, []).append(
            Message(body=body, msg_id=msg_id, correl_id=correl, put_time="20261109 120000")
        )
        return msg_id

    def get(self, queue: str) -> Message | None:
        items = self.queues.get(queue, [])
        return items.pop(0) if items else None
