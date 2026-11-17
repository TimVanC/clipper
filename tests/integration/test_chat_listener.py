import asyncio

import pytest

from app.chat import ChatLog, TwitchChatListener

pytestmark = pytest.mark.integration


async def test_listener_joins_reads_chat_and_answers_ping_then_reconnects():
    received, sessions = [], 0

    async def handle(reader, writer):
        nonlocal sessions
        sessions += 1
        first = sessions == 1
        # client handshake: PASS / NICK / JOIN
        for _ in range(3):
            received.append((await reader.readline()).decode().strip())
        writer.write(b":tmi.twitch.tv 001 justinfan1 :Welcome\r\n")
        writer.write(b"PING :tmi.twitch.tv\r\n")
        writer.write(b"@badges=;color= :a!a@a.tmi.twitch.tv PRIVMSG #speed :LUL KEKW insane\r\n")
        writer.write(b":b!b@b.tmi.twitch.tv PRIVMSG #speed :hello\r\n")
        await writer.drain()
        pong = (await reader.readline()).decode().strip()
        received.append(pong)
        if first:
            writer.close()  # force a reconnect
        else:
            await asyncio.sleep(5)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    log = ChatLog()
    ts = iter(range(1000, 1100))
    listener = TwitchChatListener("Speed", log, "127.0.0.1", port, use_ssl=False, clock=lambda: float(next(ts)))
    listener.start()
    for _ in range(100):
        if sessions >= 2 and log.total >= 4:
            break
        await asyncio.sleep(0.1)
    await listener.stop()
    server.close()
    await server.wait_closed()

    assert received[0] == "PASS SCHMOOPIIE" and received[1].startswith("NICK justinfan")
    assert received[2] == "JOIN #speed"
    assert "PONG :tmi.twitch.tv" in received
    assert sessions >= 2 and log.total >= 4          # reconnected and kept counting
    weights = sorted(w for _, w in log._events)
    assert weights[-1] > weights[0]                   # hyped message weighs more than "hello"
