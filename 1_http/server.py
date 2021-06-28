import asyncio
from urllib.parse import urlparse
from http import HTTPStatus


async def build_scope_headers(reader):
    headers = []

    while True:
        header_line = await reader.readuntil(b"\r\n")
        header = header_line.rstrip()
        if not header:
            break
        key, value = header.split(b": ", 1)
        headers.append([key.lower(), value])

    return headers


async def build_scope(reader):
    request_line = await reader.readuntil(b"\r\n")
    request = request_line.decode().rstrip()
    method, path, protocol = request.split(" ", 3)
    url = urlparse(path)
    __, http_version = protocol.split("/")
    headers = await build_scope_headers(reader)

    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": http_version,
        "method": method,
        "scheme": "http",
        "path": url.path,
        "query_string": url.query.encode(),
        "headers": headers,
    }


def get_content_length(scope):
    for [key, value] in scope["headers"]:
        if key == b"content-length":
            return int(value)

    return 0


def build_http_headers(scope, event):
    http_version = scope["http_version"]
    status = HTTPStatus(event["status"])
    status_line = f"HTTP/{http_version} {status.value} {status.phrase}\r\n"

    headers = [status_line.encode()]
    for header_line in event.get("headers", []):
        headers.append(b": ".join(header_line))
        headers.append(b"\r\n")
    headers.append(b"\r\n")

    return headers


async def handler(app, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    response_headers = []
    scope = await build_scope(reader)

    async def receive():
        # App's pulling the body of the request
        content_length = get_content_length(scope)
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


async def run_server(app, host, port):
    async def wrapped_handler(reader, writer):
        return await handler(app, reader, writer)

    server = await asyncio.start_server(wrapped_handler, host, port)
    async with server:
        await server.serve_forever()


def run(app, host, port):
    print(f"Listening on http://{host}:{port}")
    asyncio.run(run_server(app, host, port))
