import hashlib
from http import HTTPStatus
from unittest import TestCase
from unittest.mock import MagicMock, patch

import requests as requests_module

from vantage6.common.docker.addons import MANIFEST_MEDIA_TYPES, get_registry_digest

DIGEST = "sha256:" + "a" * 64
CHALLENGE = (
    'Bearer realm="https://auth.example/token",service="registry.example",'
    'scope="repository:org/algo:pull"'
)


def _response(status, headers=None, json_body=None, content=b""):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = json_body or {}
    response.content = content
    return response


class _Requests:
    """Stand-in for the requests module, keeping its exception classes"""

    RequestException = requests_module.RequestException
    Response = requests_module.Response

    def __init__(self):
        self.request = MagicMock()
        self.get = MagicMock()


class TestGetRegistryDigest(TestCase):
    def run(self, result=None):
        # pass a fresh stand-in for the requests module to every test
        with patch("vantage6.common.docker.addons.requests", _Requests()) as mock:
            self._requests = mock
            return super().run(result)

    def test_digest_reference_is_returned_as_is(self):
        requests = self._requests
        self.assertEqual(get_registry_digest(f"registry.example/algo@{DIGEST}"), DIGEST)
        self.assertEqual(
            get_registry_digest(f"registry.example/algo:1.0@{DIGEST}"), DIGEST
        )
        requests.request.assert_not_called()

    def test_anonymous_head_request_accepts_indexes(self):
        requests = self._requests
        requests.request.return_value = _response(
            HTTPStatus.OK, {"Docker-Content-Digest": DIGEST}
        )
        self.assertEqual(get_registry_digest("registry.example/org/algo:1.0"), DIGEST)

        method, url = requests.request.call_args.args
        self.assertEqual(method, "HEAD")
        self.assertEqual(url, "https://registry.example/v2/org/algo/manifests/1.0")
        accept = requests.request.call_args.kwargs["headers"]["Accept"]
        for media_type in MANIFEST_MEDIA_TYPES:
            self.assertIn(media_type, accept)

    def test_docker_hub_official_image(self):
        requests = self._requests
        requests.request.return_value = _response(
            HTTPStatus.OK, {"Docker-Content-Digest": DIGEST}
        )
        get_registry_digest("python")
        self.assertEqual(
            requests.request.call_args.args[1],
            "https://registry-1.docker.io/v2/library/python/manifests/latest",
        )

    def test_bearer_token_uses_credentials(self):
        requests = self._requests
        requests.request.side_effect = [
            _response(HTTPStatus.UNAUTHORIZED, {"www-authenticate": CHALLENGE}),
            _response(HTTPStatus.OK, {"Docker-Content-Digest": DIGEST}),
        ]
        requests.get.return_value = _response(HTTPStatus.OK, json_body={"token": "t"})

        digest = get_registry_digest("registry.example/org/algo:1.0", "user", "pass")

        self.assertEqual(digest, DIGEST)
        token_call = requests.get.call_args
        self.assertEqual(token_call.args[0], "https://auth.example/token")
        self.assertEqual(
            token_call.kwargs["params"],
            {"scope": "repository:org/algo:pull", "service": "registry.example"},
        )
        self.assertEqual(token_call.kwargs["auth"].username, "user")
        self.assertEqual(
            requests.request.call_args.kwargs["headers"]["Authorization"], "Bearer t"
        )

    def test_basic_auth_fallback(self):
        requests = self._requests
        requests.request.side_effect = [
            _response(HTTPStatus.UNAUTHORIZED),
            _response(HTTPStatus.OK, {"Docker-Content-Digest": DIGEST}),
        ]
        digest = get_registry_digest("registry.example/org/algo:1.0", "user", "pass")
        self.assertEqual(digest, DIGEST)
        self.assertEqual(requests.request.call_args.kwargs["auth"].username, "user")

    def test_digest_computed_from_manifest_without_header(self):
        requests = self._requests
        requests.request.side_effect = [
            _response(HTTPStatus.OK),
            _response(HTTPStatus.OK, content=b"manifest"),
        ]
        digest = get_registry_digest("registry.example/org/algo:1.0")
        self.assertEqual(digest, "sha256:" + hashlib.sha256(b"manifest").hexdigest())
        self.assertEqual(requests.request.call_args.args[0], "GET")

    def test_not_found(self):
        requests = self._requests
        requests.request.return_value = _response(HTTPStatus.NOT_FOUND)
        self.assertIsNone(get_registry_digest("registry.example/org/algo:1.0"))
