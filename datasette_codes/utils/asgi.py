import json
from datasette.utils import RequestParameters
from mimetypes import guess_type
from urllib.parse import parse_qs, urlunparse
from pathlib import Path
from html import escape
import re
import aiofiles


class NotFound(Exception):
    pass


class Request:
    def __init__(self, scope):
        self.scope = scope

    @property
    def method(self):
        return self.scope["method"]

    @property
    def url(self):
        return urlunparse(
            (self.scheme, self.host, self.path, None, self.query_string, None)
        )

    @property
    def scheme(self):
        return self.scope.get("scheme") or "http"

    @property
    def headers(self):
        return dict(
            [
                (k.decode("latin-1").lower(), v.decode("latin-1"))
                for k, v in self.scope.get("headers") or []
            ]
        )

    @property
    def host(self):
        return self.headers.get("host") or "localhost"

    @property
    def path(self):
        if "raw_path" in self.scope:
            return self.scope["raw_path"].decode("latin-1")
        else:
            return self.scope["path"].decode("utf-8")

    @property
    def query_string(self):
        return (self.scope.get("query_string") or b"").decode("latin-1")

    @property
    def args(self):
        return RequestParameters(parse_qs(qs=self.query_string))

    @property
    def raw_args(self):
        return {key: value[0] for key, value in self.args.items()}

    @classmethod
    def fake(cls, path_with_query_string, method="GET", scheme="http"):
        "Useful for constructing Request objects for tests"
        path, _, query_string = path_with_query_string.partition("?")
        scope = {
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": query_string.encode("latin-1"),
            "scheme": scheme,
            "type": "http",
        }
        return cls(scope)


class AsgiRouter:
    def __init__(self, routes=None):
        routes = routes or []
        self.routes = [
            # Compile any strings to regular expressions
            ((re.compile(pattern) if isinstance(pattern, str) else pattern), view)
            for pattern, view in routes
        ]

    async def __call__(self, scope, receive, send):
        # Because we care about "foo/bar" v.s. "foo%2Fbar" we decode raw_path ourselves
        path = scope["path"]
        raw_path = scope.get("raw_path")
        if raw_path:
            path = raw_path.decode("ascii")
        for regex, view in self.routes:
            match = regex.match(path)
            if match is not None:
                new_scope = dict(scope, url_route={"kwargs": match.groupdict()})
                try:
                    return await view(new_scope, receive, send)
                except Exception as exception:
                    return await self.handle_500(scope, receive, send, exception)
        return await self.handle_404(scope, receive, send)

    async def handle_404(self, scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [[b"content-type", b"text/html"]],
            }
        )
        await send({"type": "http.response.body", "body": b"<h1>404</h1>"})

    async def handle_500(self, scope, receive, send, exception):
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [[b"content-type", b"text/html"]],
            }
        )
        html = "<h1>500</h1><pre{}></pre>".format(escape(repr(exception)))
        await send({"type": "http.response.body", "body": html.encode("latin-1")})


class AsgiLifespan:
    def __init__(self, app, on_startup=None, on_shutdown=None):
        self.app = app
        on_startup = on_startup or []
        on_shutdown = on_shutdown or []
        if not isinstance(on_startup or [], list):
            on_startup = [on_startup]
        if not isinstance(on_shutdown or [], list):
            on_shutdown = [on_shutdown]
        self.on_startup = on_startup
        self.on_shutdown = on_shutdown

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    for fn in self.on_startup:
                        await fn()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    for fn in self.on_shutdown:
                        await fn()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await self.app(scope, receive, send)


class AsgiView:
    def dispatch_request(self, request, *args, **kwargs):
        handler = getattr(self, request.method.lower(), None)
        return handler(request, *args, **kwargs)

    @classmethod
    def as_asgi(cls, *class_args, **class_kwargs):
        async def view(scope, receive, send):
            # Uses scope to create a request object, then dispatches that to
            # self.get(...) or self.options(...) along with keyword arguments
            # that were already tucked into scope["url_route"]["kwargs"] by
            # the router, similar to how Django Channels works:
            # https://channels.readthedocs.io/en/latest/topics/routing.html#urlrouter
            request = Request(scope)
            self = view.view_class(*class_args, **class_kwargs)
            response = await self.dispatch_request(
                request, **scope["url_route"]["kwargs"]
            )
            await response.asgi_send(send)

        view.view_class = cls
        view.__doc__ = cls.__doc__
        view.__module__ = cls.__module__
        view.__name__ = cls.__name__
        return view


class AsgiStream:
    def __init__(self, stream_fn, status=200, headers=None, content_type="text/plain"):
        self.stream_fn = stream_fn
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type

    async def asgi_send(self, send):
        # Remove any existing content-type header
        headers = dict(
            [(k, v) for k, v in self.headers.items() if k.lower() != "content-type"]
        )
        headers["content-type"] = self.content_type
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [
                    [key.encode("utf-8"), value.encode("utf-8")]
                    for key, value in headers.items()
                ],
            }
        )
        w = AsgiWriter(send)
        await self.stream_fn(w)
        await send({"type": "http.response.body", "body": b""})


class AsgiWriter:
    def __init__(self, send):
        self.send = send

    async def write(self, chunk):
        await self.send(
            {
                "type": "http.response.body",
                "body": chunk.encode("latin-1"),
                "more_body": True,
            }
        )


async def asgi_send_json(send, info, status=200, headers=None):
    headers = headers or {}
    await asgi_send(
        send,
        json.dumps(info),
        status=status,
        headers=headers,
        content_type="application/json; charset=utf-8",
    )


async def asgi_send_html(send, html, status=200, headers=None):
    headers = headers or {}
    await asgi_send(
        send, html, status=status, headers=headers, content_type="text/html"
    )


async def asgi_send_redirect(send, location, status=302):
    await asgi_send(
        send,
        "",
        status=status,
        headers={"Location": location},
        content_type="text/html",
    )


async def asgi_send(send, content, status, headers=None, content_type="text/plain"):
    await asgi_start(send, status, headers, content_type)
    await send({"type": "http.response.body", "body": content.encode("latin-1")})


async def asgi_start(send, status, headers=None, content_type="text/plain"):
    headers = headers or {}
    # Remove any existing content-type header
    headers = dict([(k, v) for k, v in headers.items() if k.lower() != "content-type"])
    headers["content-type"] = content_type
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [key.encode("latin1"), value.encode("latin1")]
                for key, value in headers.items()
            ],
        }
    )


async def asgi_send_file(
    send, filepath, filename=None, content_type=None, chunk_size=4096
):
    headers = {}
    if filename:
        headers["Content-Disposition"] = 'attachment; filename="{}"'.format(filename)
    first = True
    async with aiofiles.open(str(filepath), mode="rb") as fp:
        if first:
            await asgi_start(
                send,
                200,
                headers,
                content_type or guess_type(str(filepath))[0] or "text/plain",
            )
            first = False
        more_body = True
        while more_body:
            chunk = await fp.read(chunk_size)
            more_body = len(chunk) == chunk_size
            await send(
                {"type": "http.response.body", "body": chunk, "more_body": more_body}
            )


def asgi_static(root_path, chunk_size=4096, headers=None, content_type=None):
    async def inner_static(scope, receive, send):
        path = scope["url_route"]["kwargs"]["path"]
        try:
            full_path = (Path(root_path) / path).resolve().absolute()
        except FileNotFoundError:
            await asgi_send_html(send, "404", 404)
            return
        # Ensure full_path is within root_path to avoid weird "../" tricks
        try:
            full_path.relative_to(root_path)
        except ValueError:
            await asgi_send_html(send, "404", 404)
            return
        try:
            await asgi_send_file(send, full_path, chunk_size=chunk_size)
        except FileNotFoundError:
            await asgi_send_html(send, "404", 404)
            return

    return inner_static


class Response:
    def __init__(self, body=None, status=200, headers=None, content_type="text/plain"):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type

    async def asgi_send(self, send):
        headers = {}
        headers.update(self.headers)
        headers["content-type"] = self.content_type
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [
                    [key.encode("utf-8"), value.encode("utf-8")]
                    for key, value in headers.items()
                ],
            }
        )
        body = self.body
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        await send({"type": "http.response.body", "body": body})

    @classmethod
    def html(cls, body, status=200, headers=None):
        return cls(
            body,
            status=status,
            headers=headers,
            content_type="text/html; charset=utf-8",
        )

    @classmethod
    def text(cls, body, status=200, headers=None):
        return cls(
            body,
            status=status,
            headers=headers,
            content_type="text/plain; charset=utf-8",
        )

    @classmethod
    def redirect(cls, path, status=302, headers=None):
        headers = headers or {}
        headers["Location"] = path
        return cls("", status=status, headers=headers)


class AsgiFileDownload:
    def __init__(
        self, filepath, filename=None, content_type="application/octet-stream"
    ):
        self.filepath = filepath
        self.filename = filename
        self.content_type = content_type

    async def asgi_send(self, send):
        return await asgi_send_file(send, self.filepath, content_type=self.content_type)
