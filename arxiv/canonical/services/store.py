"""
Persist changes to the canonical record.

Provides a :class:`.CanonicalStore` that stores resources in S3, using
:mod:`.serialize.record` to serialize and deserialize resources.
"""
import gzip
import io
import logging
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import hexlify, unhexlify
from datetime import datetime, date
from functools import partial
from hashlib import md5
from json import dumps, load
from typing import (Optional, Dict, Any, IO, List, Callable, Tuple, Type,
                    TypeVar, Union)

import boto3
import botocore
from backports.datetime_fromisoformat import MonkeyPatch
from botocore.config import Config
from botocore.exceptions import ClientError
from pytz import UTC

from .. import integrity as I
from .. import record as R
from .. import domain as D
from ..manifest import (Manifest, ManifestDecoder, ManifestEntry,
                        ManifestEncoder)
from ..register import ICanonicalStorage, IStorableEntry
from ..serialize.decoder import CanonicalDecoder
from ..serialize.encoder import CanonicalEncoder
from .readable import BytesIOProxy


MonkeyPatch.patch_fromisoformat()

logger = logging.getLogger(__name__)
logger.setLevel(int(os.environ.get('LOGLEVEL', '40')))

_I = TypeVar('_I', I.IntegrityEntry, I.IntegrityMetadata, I.IntegrityListing,
             covariant=True)

Checksum = str


class DoesNotExist(Exception):
    """The requested resource does not exist."""


class CanonicalStore(ICanonicalStorage):
    """
    Persists the canonical record in S3.

    The intended pattern for working with the canonical record is to use the
    :class:`.domain.CanonicalRecord` as the primary entrypoint for all
    operations. Consequently, this service offers only a single public instance
    method, :fund:`.load_record`.

    Persistence is achieved by attaching members to
    :class:`.domain.CanonicalRecord`, :class`.domain.Month`, and
    :class:`.domain.Listing` instances that implement reads/writes to S3. In
    this way, consumers of ``arxiv.canonical.domain`` can largely work directly
    with :class:`.domain.CanonicalRecord`, and persistence is handled
    transparently.
    """

    def __init__(self, bucket: str, verify: bool = False,
                 region_name: Optional[str] = None,
                 endpoint_url: Optional[str] = None,
                 aws_access_key_id: Optional[str] = None,
                 aws_secret_access_key: Optional[str] = None,
                 read_only: bool = True) -> None:
        """Initialize with connection config parameters."""
        self._bucket = bucket
        self._region_name = region_name
        self._endpoint_url = endpoint_url
        self._verify = verify
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._read_only = read_only
        self.client = self._new_client()

    @property
    def read_only(self) -> bool:
        """
        Determine whether or not this is a read-only session.

        This is read-only property to discourage users of this class to mess
        with it in runtime code. Should only be set via application
        configuration.
        """
        return self._read_only

    def can_resolve(self, uri: D.URI) -> bool:
        return isinstance(uri, D.Key) or uri.is_canonical

    def inititalize(self) -> None:
        self.client.create_bucket(Bucket=self._bucket)

    def is_available(self, retries: int = 0, read_timeout: int = 5,
                     connect_timeout: int = 5) -> bool:
        """Determine whether or not we can read from/write to the store."""
        raise NotImplementedError('Implement me!')

    def _load_key(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self._bucket, Key=key)
        body: IO[bytes] = response['Body']
        return body.read()

    def load(self, key: D.URI) -> IO[bytes]:
        load: Callable[[], bytes] = partial(self._load_key, key)
        return BytesIOProxy(load)

    def load_entry(self, key: D.URI) -> Tuple[R.RecordStream, Checksum]:
        assert isinstance(key, D.Key)
        logger.debug('Load entry at %s', key)
        response = self.client.get_object(Bucket=self._bucket, Key=key)
        stream = R.RecordStream(
            domain=D.CanonicalFile(
                modified=response['LastModified'],
                filename=key.filename,
                size_bytes=response['ContentLength'],
                content_type=D.ContentType.from_mimetype(response['ContentType']),
                ref=key
                # content=BytesIOProxy(response['Body'].read),
            ),
            content=BytesIOProxy(response['Body'].read),
            content_type=D.ContentType.from_mimetype(response['ContentType']),
            size_bytes=response['ContentLength']
        )
        return stream, _hex_to_b64(response['ETag'][1:-1])

    def list_subkeys(self, key: str) -> List[str]:
        response = self.client.list_objects_v2(Bucket=self._bucket, Prefix=key)
        subs = [obj['Key'].split(key, 1)[1] for obj in response['Contents']]
        return [sub.split('/', 1)[0] if '/' in sub else sub for sub in subs]

    def store_entry(self, ri: IStorableEntry) -> None:
        assert ri.record.stream.content is not None
        # Make sure to decompress the content if necessary.
        if ri.record.stream.domain.is_gzipped:
            body = gzip.GzipFile(fileobj=ri.record.stream.content).read()
            s3_checksum = _b64_to_hex(I.calculate_checksum(body))
        else:
            body = ri.record.stream.content.read()
            s3_checksum = _b64_to_hex(ri.checksum)
        size_bytes = len(body)

        self.client.put_object(Bucket=self._bucket,
                               Key=ri.record.key,
                               Body=body,
                               ContentLength=size_bytes,
                               ContentMD5=s3_checksum,
                               ContentType=ri.record.stream.content_type.mime_type)

        # Update the CanonicalFile if necessary.
        if ri.record.stream.domain.is_gzipped:
            ri.record.stream.domain.size_bytes = size_bytes
            ri.record.stream.domain.is_gzipped = False
            # Use an in-memory buffer for the checksum, to cut down on
            # unnecessary IO.
            ri.record.stream = \
                ri.record.stream._replace(content=io.BytesIO(body))
            ri.update_checksum()
            # Finally, replace the content IO with a deferred IO.
            ri.record.stream = ri.record.stream._replace(
                content=self.load(ri.record.key)
            )

    def store_manifest(self, key: str, manifest: Manifest) -> None:
        body = dumps(manifest, cls=ManifestEncoder).encode('utf-8')
        self.client.put_object(Bucket=self._bucket,
                               Key=key,
                               Body=body,
                               ContentLength=len(body),
                               ContentMD5=I.checksum.checksum_raw(body),
                               ContentType='application/json')

    def load_manifest(self, key: str) -> Manifest:
        response = self.client.get_object(Bucket=self._bucket, Key=key)
        manifest: Manifest = load(response['Body'], cls=ManifestDecoder)
        return manifest

    def _handle_client_error(self, exc: ClientError) -> None:
        if exc.response['Error']['Code'] == 'NoSuchBucket':
            raise DoesNotExist(f'{self._bucket} does not exist') from exc
        if exc.response['Error']['Code'] == "NoSuchKey":
            raise DoesNotExist(f'No such object in {self._bucket}') from exc
        raise RuntimeError('Unhandled ClientError') from exc

    def _new_client(self) -> boto3.client:
        # Only add credentials to the client if they are explicitly set.
        # If they are not set, boto3 falls back to environment variables and
        # credentials files.
        params: Dict[str, Any] = {'region_name': self._region_name}
        if self._aws_access_key_id and self._aws_secret_access_key:
            params['aws_access_key_id'] = self._aws_access_key_id
            params['aws_secret_access_key'] = self._aws_secret_access_key
        if self._endpoint_url:
            params['endpoint_url'] = self._endpoint_url
            params['verify'] = self._verify
        return boto3.client('s3', **params)


class InMemoryStorage(ICanonicalStorage):
    def __init__(self) -> None:
        self._streams: Dict[D.URI, Tuple[R.RecordStream, str]] = {}
        self._manifests: Dict[str, Manifest] = {}

    def can_resolve(self, uri: D.URI) -> bool:
        return bool(uri in self._streams)

    def load(self, key: D.URI) -> IO[bytes]:
        return self._streams[key][0].content

    def load_entry(self, key: D.URI) -> Tuple[R.RecordStream, str]:
        assert isinstance(key, D.Key)
        return self._streams[key]

    def list_subkeys(self, key: str) -> List[str]:
        return [k.split(key, 1)[1].split('/', 1)[0]
                for k in self._streams.keys()
                if k.startswith(key) and k != key]

    def store_entry(self, ri: IStorableEntry) -> None:
        assert ri.record.stream.content is not None
        if ri.record.stream.domain.is_gzipped:
            content = gzip.GzipFile(fileobj=ri.record.stream.content).read()
            ri.record.stream.domain.size_bytes = len(content)
            ri.record.stream.domain.is_gzipped = False
            ri.record.stream = \
                ri.record.stream._replace(content=io.BytesIO(content))
            ri.update_checksum()
        self._streams[ri.record.key] = (ri.record.stream, ri.checksum)
        ri.record.stream.domain.ref = ri.record.key

    def store_manifest(self, key: str, manifest: Manifest) -> None:
        self._manifests[key] = manifest

    def load_manifest(self, key: str) -> Manifest:
        return self._manifests[key]


def _b64_to_hex(checksum: Checksum) -> str:
    return hexlify(urlsafe_b64decode(checksum.encode('utf-8'))).decode('utf-8')


def _hex_to_b64(etag: str) -> Checksum:
    """Convert an hexdigest of an MD5 to a URL-safe base64-encoded digest."""
    return urlsafe_b64encode(unhexlify(etag)).decode('utf-8')

