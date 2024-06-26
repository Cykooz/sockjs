from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_coro

from sockjs.transports import EventsourceTransport


@pytest.fixture
def make_transport(make_manager, make_request, make_handler, make_fut):
    def maker(method="GET", path="/", query_params=None):
        handler = make_handler(None)
        manager = make_manager(handler)
        request = make_request(method, path, query_params=query_params)
        request.app.freeze()
        session = manager.get("TestSessionEventSource", create=True)
        return EventsourceTransport(manager, session, request)

    return maker


async def test_streaming_send(make_transport):
    trans = make_transport()

    resp = trans.response = mock.Mock()
    resp.write = make_mocked_coro(None)
    stop = await trans._send("text data")
    resp.write.assert_called_with(b"data: text data\r\n\r\n")
    assert not stop
    assert trans.size == len(b"data: text data\r\n\r\n")

    trans.maxsize = 1
    stop = await trans._send("text data")
    assert stop


async def test_process(make_transport, make_fut):
    transp = make_transport()
    transp.handle_session = make_fut(1)
    resp = await transp.process()
    assert transp.handle_session.called
    assert resp.status == 200
