"""aiohttp-shaped fake for node/explorer GETs: routes map a URL suffix to (status, json)."""


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """`routes`: {url_suffix: (status, body) | Exception}. Unknown URLs return 404."""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.calls: list[str] = []

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        for suffix, result in self.routes.items():
            if url.endswith(suffix):
                if isinstance(result, BaseException):
                    raise result
                return _Resp(*result)
        return _Resp(404, {"error": 404})
