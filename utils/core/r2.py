"""Cloudflare R2 storage for the Mixtapes library.

R2 speaks the S3 API, so this is a small SigV4 client built on the ``aiohttp``
the bot already depends on — nothing extra to install. It covers the four calls
the library needs: list a prefix, check a key, download one, upload one.

Configuration is entirely by environment variable, and the whole feature stays
off until every required one is set:

    R2_ACCOUNT_ID          the account id from the R2 dashboard
    R2_ACCESS_KEY_ID       an R2 API token's access key
    R2_SECRET_ACCESS_KEY   its secret
    R2_BUCKET              the bucket holding ``Mixtapes/``, by *name*
                           (``slimey``), not its S3 endpoint URL

Optional:

    R2_PUBLIC_BASE_URL     the bucket's custom subdomain, e.g.
                           ``https://cdn.example.com``. Links are built from
                           this; without it they are presigned instead.
    R2_PREFIX              the folder the mixtapes live in (default
                           ``Mixtapes``), so the bucket can hold other things.
    R2_CACHE_DIR           where downloaded files and archives are kept
                           (default ``.cache/mixtapes``).
    R2_PRESIGN_SECONDS     how long a presigned link stays valid (default
                           ``3600``).

With those set the bot indexes ``<R2_PREFIX>/<Project>/MP3/*`` and serves each
project's ``<R2_PREFIX>/<Project>/ZIP/*.zip`` from the subdomain, so nothing has
to fit inside Discord's upload limit.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import logging
import os
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger("slimey.r2")

SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
#: Cloudflare signs against ``auto`` rather than a real AWS region.
DEFAULT_REGION = "auto"
_XML_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


REQUIRED_ENV = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _normalize_bucket(value: str) -> str:
    """The bucket name inside whatever was put in ``R2_BUCKET``.

    Meant to hold nothing but the name (``slimey``), but the R2 dashboard shows
    the S3 endpoint right next to it, so that URL gets pasted in constantly.
    A full endpoint or ``host/bucket`` is accepted and unwrapped; an endpoint
    on its own carries no name at all, and ``""`` says so — signing requests
    against it would only produce a 404 per listing, which is a far more
    confusing failure than saying the value is wrong.
    """
    candidate = value.strip().strip("/")
    if "://" in candidate:
        candidate = candidate.split("://", 1)[1]
    host, _, path = candidate.partition("/")
    if path:
        return path.split("/")[0]
    if "." in host:
        # A hostname rather than a name: R2 bucket names cannot contain dots,
        # so this is the endpoint with nothing to recover from it.
        return ""
    return candidate


def config_note() -> str | None:
    """Why the bucket isn't in use, or ``None`` when it is (or never was set).

    Only speaks up when the variables are there but unusable — a bot with no
    R2 setup at all reads the local folder and has nothing to explain. The
    ``!library`` card shows this, so a misconfigured bucket reports itself
    instead of looking like an empty library.
    """
    values = {name: _env(name) for name in REQUIRED_ENV}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        return (
            "The bucket is only half configured — set "
            + " and ".join(f"`{name}`" for name in missing)
            + " to read the library from R2."
        )
    if not _normalize_bucket(values["R2_BUCKET"]):
        return (
            f"`R2_BUCKET` is set to the S3 endpoint (`{values['R2_BUCKET']}`) "
            "rather than a bucket name, so nothing can be listed. It wants just "
            "the name of the bucket holding `Mixtapes/`, e.g. "
            "`R2_BUCKET=slimey` — the account id is already covered by "
            "`R2_ACCOUNT_ID`."
        )
    return None


@dataclass(slots=True, frozen=True)
class StoredObject:
    """One object in the bucket: its key and its size in bytes."""

    key: str
    size: int


@dataclass(slots=True, frozen=True)
class R2Config:
    """Where the bucket is and how to prove we may read it."""

    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str = ""
    prefix: str = "Mixtapes"
    region: str = DEFAULT_REGION
    presign_expiry: int = 3600

    @classmethod
    def from_env(cls) -> "R2Config | None":
        """Read the config from the environment, or ``None`` when it is off.

        All four required values have to be present: a half-configured bucket
        is far more confusing than no bucket at all. ``R2_BUCKET`` is unwrapped
        first, so an endpoint pasted out of the R2 dashboard is caught here
        rather than turning into a 404 on every listing (see
        :func:`_normalize_bucket`).
        """
        account_id = _env("R2_ACCOUNT_ID")
        access_key_id = _env("R2_ACCESS_KEY_ID")
        secret_access_key = _env("R2_SECRET_ACCESS_KEY")
        bucket = _normalize_bucket(_env("R2_BUCKET"))
        if not all((account_id, access_key_id, secret_access_key, bucket)):
            # ``config_note()`` turns this into the reason the bot can show.
            logger.error(
                "R2 is configured but unusable (%s); reading the local folder",
                config_note() or "an R2_* value is empty",
            )
            return None

        try:
            presign_expiry = int(_env("R2_PRESIGN_SECONDS", "3600") or 3600)
        except ValueError:
            presign_expiry = 3600

        return cls(
            account_id=account_id,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket=bucket,
            public_base_url=_env("R2_PUBLIC_BASE_URL").rstrip("/"),
            prefix=_env("R2_PREFIX", "Mixtapes").strip("/"),
            presign_expiry=presign_expiry,
        )

    @property
    def label(self) -> str:
        """``bucket/prefix``, for logs and the ``!library`` card."""
        return f"{self.bucket}/{self.prefix}" if self.prefix else self.bucket


class R2Client:
    """A tiny S3 client, signed for Cloudflare R2."""

    def __init__(self, config: R2Config) -> None:
        self.config = config
        #: Why the last call failed, so ``!library`` can report the status code
        #: instead of just showing an empty library.
        self.last_error: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    # -- plumbing --------------------------------------------------------
    @property
    def host(self) -> str:
        return f"{self.config.account_id}.r2.cloudflarestorage.com"

    async def session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def key_for(self, relative: str) -> str:
        """Turn ``Project/MP3/track.mp3`` into a full bucket key."""
        relative = relative.strip("/")
        prefix = self.config.prefix
        if not prefix:
            return relative
        return f"{prefix}/{relative}" if relative else prefix

    def public_url(self, key: str) -> str | None:
        """The bucket's own URL for ``key``, or ``None`` without a domain."""
        if not self.config.public_base_url:
            return None
        return f"{self.config.public_base_url}/{_encode_key(key)}"

    async def download_url(self, key: str, filename: str | None = None) -> str | None:
        """A link that *saves* the file instead of playing it in a tab.

        With a public domain this is the bucket's own link: the object is stored
        with a ``Content-Disposition: attachment`` header (see :meth:`put`), and
        the bucket serves that header back, so the click downloads. Without a
        public domain a presigned URL carries the same override instead — that
        one expires with the presign lifetime, so keep
        ``R2_PUBLIC_BASE_URL`` set for links that have to keep working.
        """
        url = self.public_url(key)
        if url:
            return url
        return await self.presign(key, download_filename=filename)

    # -- signing ---------------------------------------------------------
    def _signing_key(self, date_stamp: str) -> bytes:
        secret = f"AWS4{self.config.secret_access_key}".encode()
        key = hmac.new(secret, date_stamp.encode(), hashlib.sha256).digest()
        key = hmac.new(key, self.config.region.encode(), hashlib.sha256).digest()
        key = hmac.new(key, SERVICE.encode(), hashlib.sha256).digest()
        return hmac.new(key, b"aws4_request", hashlib.sha256).digest()

    def _canonical_uri(self, key: str) -> str:
        bucket = urllib.parse.quote(self.config.bucket, safe="")
        if not key:
            return f"/{bucket}"
        return f"/{bucket}/{_encode_key(key)}"

    def _signature(
        self,
        *,
        method: str,
        canonical_uri: str,
        canonical_query: str,
        canonical_headers: str,
        signed_headers: str,
        payload_hash: str,
        amz_date: str,
        date_stamp: str,
    ) -> str:
        scope = f"{date_stamp}/{self.config.region}/{SERVICE}/aws4_request"
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        string_to_sign = "\n".join(
            [
                ALGORITHM,
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        return hmac.new(
            self._signing_key(date_stamp), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()

    async def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: dict[str, str] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        content_disposition: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> aiohttp.ClientResponse:
        """Send one signed request. The caller reads (and releases) it."""
        query = {name: str(value) for name, value in (query or {}).items()}
        payload = body or b""
        payload_hash = hashlib.sha256(payload).hexdigest()

        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        canonical_uri = self._canonical_uri(key)
        canonical_query = "&".join(
            f"{_quote(name)}={_quote(value)}"
            for name, value in sorted(query.items())
        )

        headers = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            headers["content-type"] = content_type
        if content_disposition:
            # Sent and signed like any other header, which is the only way the
            # bucket will store it on the object.
            headers["content-disposition"] = content_disposition
        for name, value in (extra_headers or {}).items():
            headers[name.lower()] = value
        # SigV4 canonicalisation trims surrounding space and collapses runs of
        # spaces inside a value, so an unnormalised header would be signed one
        # way and compared another — a 403 SignatureDoesNotMatch on every PUT
        # whose filename happened to hold a double space.
        headers = {name: " ".join(str(value).split()) for name, value in headers.items()}
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(
            f"{name}:{value}\n" for name, value in sorted(headers.items())
        )

        signature = self._signature(
            method=method,
            canonical_uri=canonical_uri,
            canonical_query=canonical_query,
            canonical_headers=canonical_headers,
            signed_headers=signed_headers,
            payload_hash=payload_hash,
            amz_date=amz_date,
            date_stamp=date_stamp,
        )
        scope = f"{date_stamp}/{self.config.region}/{SERVICE}/aws4_request"
        authorization = (
            f"{ALGORITHM} Credential={self.config.access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        # The query string is hand-built above and signed, so it must go on the
        # URL as-is; aiohttp would re-encode it differently and break the
        # signature.
        url = f"https://{self.host}{canonical_uri}"
        if canonical_query:
            url = f"{url}?{canonical_query}"

        send_headers = {
            name: value for name, value in headers.items() if name != "host"
        }
        send_headers["Authorization"] = authorization

        session = await self.session()
        return await session.request(
            method, url, data=body if body else None, headers=send_headers
        )

    # -- operations ------------------------------------------------------
    async def list_objects(self, prefix: str = "") -> list[StoredObject] | None:
        """Every object under ``prefix``, or ``None`` if the listing failed.

        The two are deliberately different: an empty bucket must be allowed to
        empty the index, while a network blip must not.
        """
        objects: list[StoredObject] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                query["prefix"] = prefix if prefix.endswith("/") else f"{prefix}/"
            if token:
                query["continuation-token"] = token

            async with await self._request("GET", query=query) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    logger.error(
                        "R2 listing failed (%s): %s", response.status, detail
                    )
                    self.last_error = self._describe_failure(
                        f"listing `{prefix or '/'}`", response.status, detail
                    )
                    return None
                payload = await response.read()

            try:
                root = ET.fromstring(payload)
            except ET.ParseError:
                logger.error("R2 returned a listing that could not be parsed")
                self.last_error = (
                    "R2 answered the listing with something that wasn't XML."
                )
                return None

            for node in root.findall("s3:Contents", _XML_NS):
                key = node.findtext("s3:Key", default="", namespaces=_XML_NS)
                if not key:
                    continue
                raw_size = node.findtext("s3:Size", default="0", namespaces=_XML_NS)
                objects.append(StoredObject(key=key, size=_as_int(raw_size)))

            truncated = (
                node_text(root, "s3:IsTruncated") or ""
            ).lower() == "true"
            if not truncated:
                self.last_error = None
                return objects

            token = node_text(root, "s3:NextContinuationToken") or None
            if not token:
                self.last_error = None
                return objects

    @staticmethod
    def _describe_failure(action: str, status: int, detail: str) -> str:
        """A one-liner for the ``!library`` card, with the usual cause named."""
        hint = {
            400: " Check `R2_ACCOUNT_ID` and `R2_BUCKET`.",
            403: " The API token has no read access to this bucket.",
            404: " Check `R2_BUCKET` and `R2_PREFIX` against the bucket.",
        }.get(status, "")
        return f"R2 kicked back the {action} with HTTP {status}: {detail}{hint}"

    async def headers(self, key: str) -> dict[str, str] | None:
        """The response headers for ``key``, or ``None`` when it is missing.

        Used to read back what an object already carries (its content type in
        particular) before rewriting its metadata.
        """
        async with await self._request("HEAD", key) as response:
            if response.status != 200:
                return None
            return {name.lower(): value for name, value in response.headers.items()}

    async def mark_as_download(
        self,
        key: str,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> bool:
        """Make ``key``'s *existing* object download from its link.

        Rewrites the object's metadata in place — the bytes are copied
        server-side, so nothing is re-uploaded and an ETag check proves the
        file itself is untouched. The old metadata is replaced wholesale, so
        pass the object's current ``content_type`` back in or it is lost.
        """
        if not filename:
            filename = key.rsplit("/", 1)[-1]
        async with await self._request(
            "PUT",
            key,
            content_type=content_type,
            content_disposition=content_disposition(filename),
            extra_headers={
                "x-amz-copy-source": f"/{self.config.bucket}/{_encode_key(key)}",
                "x-amz-metadata-directive": "REPLACE",
            },
        ) as response:
            if response.status != 200:
                detail = (await response.text())[:300]
                logger.error(
                    "R2 could not rewrite %s (%s): %s", key, response.status, detail
                )
                return False
        return True

    async def head(self, key: str) -> StoredObject | None:
        """The object's size, or ``None`` when it isn't there."""
        async with await self._request("HEAD", key) as response:
            if response.status != 200:
                return None
            length = response.headers.get("Content-Length", "")
            return StoredObject(key=key, size=_as_int(length))

    async def delete(self, key: str) -> bool:
        """Remove one object. ``True`` when the bucket accepted the delete."""
        async with await self._request("DELETE", key) as response:
            if response.status not in (200, 204):
                detail = (await response.text())[:300]
                logger.error(
                    "R2 could not delete %s (%s): %s", key, response.status, detail
                )
                return False
        return True

    async def get(self, key: str) -> bytes | None:
        """Download one object."""
        async with await self._request("GET", key) as response:
            if response.status != 200:
                logger.warning("R2 could not read %s (%s)", key, response.status)
                return None
            return await response.read()

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        content_disposition: str | None = None,
    ) -> bool:
        """Upload one object, overwriting whatever was there.

        Pass ``content_disposition`` (:func:`content_disposition`) to make the
        object download rather than preview when its link is opened.
        """
        async with await self._request(
            "PUT",
            key,
            body=data,
            content_type=content_type,
            content_disposition=content_disposition,
        ) as response:
            if response.status not in (200, 201):
                detail = (await response.text())[:300]
                logger.error("R2 could not write %s (%s): %s", key, response.status, detail)
                return False
        return True

    async def presign(
        self,
        key: str,
        expires: int | None = None,
        download_filename: str | None = None,
    ) -> str | None:
        """A time-limited URL for a private bucket.

        Only used when the bucket has no public base URL; a custom subdomain is
        the better answer because the link never expires. Pass
        ``download_filename`` to have the URL save the file rather than play it
        (the override is part of the signature, which is why it is signed here).
        """
        lifetime = int(expires or self.config.presign_expiry)
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        scope = f"{date_stamp}/{self.config.region}/{SERVICE}/aws4_request"

        query = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{self.config.access_key_id}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(lifetime),
            "X-Amz-SignedHeaders": "host",
        }
        if download_filename:
            query["response-content-disposition"] = content_disposition(
                download_filename
            )
        canonical_uri = self._canonical_uri(key)
        canonical_query = "&".join(
            f"{_quote(name)}={_quote(value)}" for name, value in sorted(query.items())
        )
        signature = self._signature(
            method="GET",
            canonical_uri=canonical_uri,
            canonical_query=canonical_query,
            canonical_headers=f"host:{self.host}\n",
            signed_headers="host",
            payload_hash=UNSIGNED_PAYLOAD,
            amz_date=amz_date,
            date_stamp=date_stamp,
        )
        return (
            f"https://{self.host}{canonical_uri}?{canonical_query}"
            f"&X-Amz-Signature={signature}"
        )


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """A ``Content-Disposition`` value that saves ``filename`` as given.

    A plain ASCII ``filename`` keeps older clients working, and the RFC 5987
    ``filename*`` alongside it carries the real name, so a title with emoji or
    accents still arrives with the right characters.
    """
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip() or "audio.mp3"
    paper = (
        name.encode("ascii", "ignore")
        .decode()
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
    )
    paper = " ".join(paper.split()) or "audio.mp3"
    return f"{disposition}; filename=\"{paper}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def _quote(value: str) -> str:
    """S3-style percent encoding: everything but the unreserved set."""
    return urllib.parse.quote(value, safe="-_.~")


def _encode_key(key: str) -> str:
    """Percent-encode a key while keeping its ``/`` separators intact."""
    return "/".join(_quote(part) for part in key.split("/"))


def _as_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def node_text(root: ET.Element, path: str) -> str | None:
    return root.findtext(path, default=None, namespaces=_XML_NS)
