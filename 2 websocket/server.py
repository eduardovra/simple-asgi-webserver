import asyncio
from urllib.parse import urlparse
from http import HTTPStatus
import struct
import hashlib
import base64


async def build_scope_headers(reader):
    headers = []
    dict_headers = {}

    while True:
        header_line = await reader.readuntil(b"\r\n")
        header = header_line.rstrip()
        if not header:
            break
        key, value = header.split(b": ", 1)
        headers.append([key.lower(), value])
        dict_headers[key.lower().decode()] = value.decode()

    return headers, dict_headers


async def build_scope(reader):
    request_line = await reader.readuntil(b"\r\n")
    request = request_line.decode().rstrip()
    method, path, protocol = request.split(" ", 3)
    url = urlparse(path)
    __, http_version = protocol.split("/")
    headers, dict_headers = await build_scope_headers(reader)

    scope = {
        "asgi": {"version": "3.0"},
        "http_version": http_version,
        "scheme": "http",
        "path": url.path,
        "query_string": url.query.encode(),
        "headers": headers,
        "server.dict_headers": dict_headers,  # Internal use only
    }

    if (
        "Upgrade" in dict_headers.get("connection", "")
        and dict_headers.get("upgrade") == "websocket"
    ):
        scope.update({"type": "websocket"})
    else:
        scope.update({"type": "http", "method": method})

    return scope


def build_http_headers(scope, event):
    http_version = scope["http_version"]
    status = HTTPStatus(event["status"])
    status_line = f"HTTP/{http_version} {status.value} {status.phrase}\r\n"

    headers = [status_line.encode()]
    for header_line in event["headers"]:
        headers.append(b": ".join(header_line))
        headers.append(b"\r\n")
    headers.append(b"\r\n")

    return headers


async def http_handler(app, scope, reader, writer):
    response_headers = []

    async def receive():
        # App's pulling the body of the request
        dict_headers = scope["server.dict_headers"]
        content_length = int(dict_headers.get("content-length", 0))
        return {
            "type": "http.request",
            "body": await reader.read(content_length),
            "more_body": False,
        }

    async def send(event):
        nonlocal response_headers

        # App's sending the response headers
        if event["type"] == "http.response.start":
            response_headers = build_http_headers(scope, event)
        # App's sending the response body
        elif event["type"] == "http.response.body":
            # The headers should only be pushed once the body is received
            if response_headers:
                writer.writelines(response_headers)
                response_headers = []

            writer.write(event["body"])
            await writer.drain()

            if event.get("more_body", False) is False:
                # Close connection
                writer.close()
                await writer.wait_closed()

    # Invoke the app!
    await app(scope, receive, send)


async def read_websocket_frame(reader):
    # Read frame header
    header = await reader.read(2)

    unpacked = struct.unpack("<BB", header)
    fin = (unpacked[0] & (1 << 7)) > 0
    opcode = unpacked[0] & 0x0F
    mask = (unpacked[1] & (1 << 7)) > 0
    payload_len = unpacked[1] & 0x7F

    if payload_len == 126:
        l = await reader.read(2)
        u = struct.unpack("<H", l)
        payload_len = u[0]
    elif payload_len == 127:
        l = await reader.read(8)
        u = struct.unpack("<Q", l)
        payload_len = u[0]

    if mask:
        masking_key = await reader.read(4)

    payload = await reader.read(payload_len)

    if mask:
        unmasked = bytearray()
        for i in range(len(payload)):
            unmasked.append(payload[i] ^ masking_key[i % 4])
        payload = bytes(unmasked)

    return fin, opcode, payload


def build_upgrade_response(scope):
    # Complete HTTP handshake and upgrade to websocket connection
    key = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    dict_headers = scope["server.dict_headers"]
    i = dict_headers["sec-websocket-key"]
    h = hashlib.sha1(f"{i}{key}".encode())
    accept = base64.b64encode(h.digest()).decode()

    headers = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept}",
        "",
    ]

    return [f"{line}\r\n".encode() for line in headers]


def build_websocket_frame(event):
    frame = bytearray()
    payload = b""

    if event.get("text") is not None:
        payload = event["text"].encode()
        frame.extend(bytes([(1 << 7) | 1]))  # opcode 1 + FIN
    elif event.get("bytes") is not None:
        payload = event["bytes"]
        frame.extend(bytes([(1 << 7) | 2]))  # opcode 2 + FIN

    # Send payload length
    if len(payload) <= 125:
        frame.extend(bytes([len(payload)]))
    elif len(payload) < 0xFFFF:
        l = len(payload).to_bytes(2, "big")
        frame.extend(bytes([126]) + l)
    else:
        l = len(payload).to_bytes(8, "big")
        frame.extend(bytes([127]) + l)

    frame.extend(payload)

    return frame


async def websocket_handler(app, scope, reader, writer):
    connect_sent = False
    fragmented_payload = {}

    async def receive():
        nonlocal connect_sent
        nonlocal fragmented_payload

        if not connect_sent:
            connect_sent = True
            return {"type": "websocket.connect"}

        fin, opcode, payload = await read_websocket_frame(reader)

        if opcode == 0:
            if "text" in fragmented_payload:
                fragmented_payload["text"] += payload.decode()
            else:
                fragmented_payload["bytes"] += payload
        elif opcode == 1:
            fragmented_payload = {"text": payload.decode()}
        elif opcode == 2:
            fragmented_payload = {"bytes": payload}
        elif opcode == 8:
            close_code = 1005  # Default
            if len(payload):
                u = struct.unpack(">H", payload)
                close_code = u[0]
            return {"type": "websocket.disconnect", "code": close_code}

        if fin:
            event = {"type": "websocket.receive", **fragmented_payload}
            return event

        # Recurse to fetch all fragments
        return await receive()

    async def send(event):
        if event["type"] == "websocket.accept":
            # App's is requesting to accept the connection request
            response = build_upgrade_response(scope)
            writer.writelines(response)
            await writer.drain()
        elif event["type"] == "websocket.send":
            # App's sending data
            frame = build_websocket_frame(event)
            writer.write(frame)
            await writer.drain()
        elif event["type"] == "websocket.close":
            # App's requesting to close the connection
            code = event.get("code", 1000)
            writer.write(bytes([1 << 7 | 8]))  # opcode 8
            writer.write(bytes([2]) + code.to_bytes(2, "big"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    # Invoke the app!
    await app(scope, receive, send)


async def handler(app, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    scope = await build_scope(reader)

    if scope["type"] == "http":
        await http_handler(app, scope, reader, writer)
    elif scope["type"] == "websocket":
        await websocket_handler(app, scope, reader, writer)


async def run_server(app, host, port):
    async def wrapped_handler(reader, writer):
        return await handler(app, reader, writer)

    server = await asyncio.start_server(wrapped_handler, host, port)
    async with server:
        await server.serve_forever()


def run(app, host, port):
    print(f"Listening on http://{host}:{port}")
    asyncio.run(run_server(app, host, port))
