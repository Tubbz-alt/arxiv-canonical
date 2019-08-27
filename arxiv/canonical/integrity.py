
import datetime
from base64 import urlsafe_b64encode
from hashlib import md5
from operator import attrgetter, itemgetter
from typing import IO, NamedTuple, List, Dict, Sequence, Optional, Tuple, \
    Mapping, Generic, TypeVar

from mypy_extensions import TypedDict

from .domain import VersionedIdentifier, Identifier
from .serialize.record import RecordEntry, RecordListing, RecordVersion


class ManifestEntry(TypedDict, total=False):
    key: str
    checksum: Optional[str]
    size_bytes: int
    mime_type: str


class Manifest(TypedDict):
    entries: List[ManifestEntry]


def io_checksum(content: IO[bytes]) -> str:
    """Generate an URL-safe base64-encoded md5 hash of an IO."""
    if content.seekable:
        content.seek(0)     # Make sure that we are at the start of the stream.
    hash_md5 = md5()
    for chunk in iter(lambda: content.read(4096), b""):
        hash_md5.update(chunk)
    if content.seekable:
        content.seek(0)     # Be a good neighbor for subsequent users.
    return urlsafe_b64encode(hash_md5.digest()).decode('utf-8')


def get_checksum(manifest: Manifest) -> str:
    components: List[str] = []
    for entry in sorted(manifest['entries'], key=itemgetter('key')):
        if 'checksum' not in entry or entry['checksum'] is None:
            raise ChecksumError(f'Missing checksum: {entry}')
        components.append(entry['checksum'])
    return checksum_raw(''.join(components).encode('utf-8'))


class BaseIntegrity:
    def __init__(self, manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        self._manifest = manifest
        self._checksum = checksum

    @property
    def checksum(self) -> str:
        assert self._checksum is not None
        return self._checksum

    @property
    def manifest(self) -> Manifest:
        assert self._manifest is not None
        return self._manifest

    def update_checksum(self, checksum: str) -> None:
        self._checksum = checksum


class IntegrityEntry(BaseIntegrity):
    def __init__(self, record: RecordEntry,
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityEntry, self).__init__(manifest, checksum)
        self.record = record

    @property
    def manifest_entry(self) -> ManifestEntry:
        return {'key': self.record.key,
                'checksum': self.checksum,
                'size_bytes': self.record.size_bytes,
                'mime_type': self.record.content_type.mime_type}

    @classmethod
    def get_checksum(cls, record: RecordEntry) -> str:
        assert record.content is not None
        return io_checksum(record.content)

    @classmethod
    def from_record(cls, record: RecordEntry) -> 'IntegrityEntry':
        return cls(record=record, checksum=cls.get_checksum(record))

    @property
    def is_valid(self) -> bool:
        return bool(self.checksum == self.get_checksum(self.record))


# This may seem a little odd, but want to leave the possibility of multiple
# listing files per day.
class IntegrityListing(BaseIntegrity):
    def __init__(self, date: datetime.date, listing: IntegrityEntry,
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityListing, self).__init__(manifest, checksum)
        self.date = date
        self.listing = listing

    @classmethod
    def make_manifest(cls, listing: IntegrityEntry) -> Manifest:
        return Manifest(entries=[listing.manifest_entry])

    @classmethod
    def from_record(cls, record: RecordListing) -> 'IntegrityListing':
        entry = IntegrityEntry.from_record(record.listing)
        manifest = cls.make_manifest(entry)
        return cls(record.date, entry, manifest=manifest,
                   checksum=get_checksum(manifest))

    @property
    def manifest_key(self) -> str:
        return self.date.isoformat()

    @property
    def is_valid(self) -> bool:
        assert self.manifest is not None
        return bool(self.checksum == get_checksum(self.manifest))

    def validate(self) -> None:
        if not self.is_valid:
            raise ChecksumError('Loaded checksum does not match')


class IntegrityListingRange(NamedTuple):
    start_date: datetime.date
    end_date: datetime.date
    listings: Sequence[IntegrityListing]
    manifest: Optional[Manifest] = None
    checksum: Optional[str] = None


class IntegrityListingMonth(BaseIntegrity):
    def __init__(self, year: int, month: int,
                 days: Mapping[datetime.date, IntegrityListing],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityListingMonth, self).__init__(manifest, checksum)
        self.year = year
        self.month = month
        self.days = days

    @classmethod
    def make_manifest_entry(cls, day: IntegrityListing) -> ManifestEntry:
        return {'key': day.manifest_key, 'checksum': day.checksum}

    @classmethod
    def make_manifest(cls, days: Mapping[datetime.date, IntegrityListing]) \
            -> Manifest:
        return Manifest(entries=[cls.make_manifest_entry(days[day])
                                 for day in days])

    @property
    def is_valid(self) -> bool:
        assert self.manifest is not None
        return bool(self.checksum == get_checksum(self.manifest))

    @property
    def manifest_key(self) -> str:
        return f'{self.year}-{str(self.month).zfill(2)}'


class IntegrityListingYear(BaseIntegrity):
    def __init__(self, year: int,
                 months: Mapping[datetime.date, IntegrityListingMonth],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityListingYear, self).__init__(manifest, checksum)
        self.year = year
        self.months = months

    @classmethod
    def make_manifest(cls, months: Mapping[Tuple[int, int],
                                           IntegrityListingMonth]) -> Manifest:
        return Manifest(entries=[
            {'key': months[(year, month)].manifest_key,
             'checksum': months[(year, month)].checksum}
            for year, month in months
        ])

    @property
    def is_valid(self) -> bool:
        return bool(self.checksum == get_checksum(self.manifest))

    @property
    def manifest_key(self) -> str:
        return str(self.year)


class IntegrityAllListings(BaseIntegrity):
    def __init__(self, years: Mapping[int, IntegrityListingYear],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityAllListings, self).__init__(manifest, checksum)
        self.years = years

    @classmethod
    def make_manifest(cls, years: Mapping[int, IntegrityListingYear]) \
            -> Manifest:
        return Manifest(entries=[
            {'key': years[year].manifest_key, 'checksum': years[year].checksum}
            for year in years
        ])

    @property
    def is_valid(self) -> bool:
        return bool(self.checksum == get_checksum(self.manifest))

    @property
    def manifest_key(self) -> str:
        return 'listings'


class IntegrityVersion(BaseIntegrity):
    def __init__(self, identifier: VersionedIdentifier,
                 metadata: IntegrityEntry,
                 render: IntegrityEntry,
                 source: IntegrityEntry,
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityVersion, self).__init__(manifest, checksum)
        self.identifier = identifier
        self.metadata = metadata
        self.render = render
        self.source = source

    @classmethod
    def make_manifest(cls, metadata: IntegrityEntry, render: IntegrityEntry,
                     source: IntegrityEntry) -> Manifest:
        return Manifest(entries=[metadata.manifest_entry,
                                 render.manifest_entry,
                                 source.manifest_entry])

    @classmethod
    def from_record(cls, version: RecordVersion) -> 'IntegrityVersion':
        metadata = IntegrityEntry.from_record(version.metadata)
        render = IntegrityEntry.from_record(version.render)
        source = IntegrityEntry.from_record(version.source)
        manifest = cls.make_manifest(metadata, render, source)
        return cls(identifier=version.identifier,
                   metadata=metadata,
                   render=render,
                   source=source,
                   manifest=manifest,
                   checksum=get_checksum(manifest))

    def iter_entries(self) -> Sequence[IntegrityEntry]:
        return [self.metadata, self.render, self.source]

    @property
    def is_valid(self) -> bool:
        assert self.manifest is not None
        return bool(self.checksum == get_checksum(self.manifest))


class IntegrityEPrint(BaseIntegrity):
    def __init__(self, identifier: VersionedIdentifier,
                 versions: Sequence[IntegrityVersion],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityEPrint, self).__init__(manifest, checksum)
        self.identifier = identifier
        self.versions = versions

    @classmethod
    def make_manifest(cls, versions: Sequence[IntegrityVersion]) -> Manifest:
        return Manifest(entries=[{'key': v.identifier, 'checksum': v.checksum}
                                 for v in versions])

    @property
    def is_valid(self) -> bool:
        assert self.manifest is not None
        return bool(self.checksum == get_checksum(self.manifest))


class IntegrityDay(BaseIntegrity):
    def __init__(self, date: datetime.date,
                 eprints: Mapping[Identifier, IntegrityEPrint],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityDay, self).__init__(manifest, checksum)
        self.date = date
        self.eprints = eprints

    @classmethod
    def make_manifest(cls, eprints: Mapping[Identifier, IntegrityEPrint]) \
            -> Manifest:
        return Manifest(entries=[{'key': i, 'checksum': eprints[i].checksum}
                                 for i in eprints])


class IntegrityMonth(BaseIntegrity):
    def __init__(self, year: int, month: int,
                 days: Mapping[datetime.date, IntegrityDay],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityMonth, self).__init__(manifest, checksum)
        self.year = year
        self.month = month
        self.days = days

    @classmethod
    def make_manifest(cls, days: Mapping[datetime.date, IntegrityDay]) \
            -> Manifest:
        return Manifest(entries=[{'key': days[day].date.isoformat(),
                                  'checksum': days[day].checksum}
                                 for day in days])


class IntegrityYear(BaseIntegrity):
    def __init__(self, year: int,
                 months: Mapping[Tuple[int, int], IntegrityMonth],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityYear, self).__init__(manifest, checksum)
        self.year = year
        self.months = months

    @classmethod
    def make_manifest(cls, months: Mapping[Tuple[int, int], IntegrityMonth]) \
            -> Manifest:
        return Manifest(entries=[{'key': f'{year}-{str(month).zfill(2)}',
                                  'checksum': months[(year, month)].checksum}
                                 for year, month in months])


class IntegrityAllEPrints(BaseIntegrity):
    def __init__(self, years: Sequence[IntegrityYear],
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityAllEPrints, self).__init__(manifest, checksum)
        self.years = years

    @classmethod
    def make_manifest(cls, months: Mapping[Tuple[int, int], IntegrityMonth]) \
            -> Manifest:
        return Manifest(entries=[{'key': f'{year}-{str(month).zfill(2)}',
                                  'checksum': months[(year, month)].checksum}
                                 for year, month in months])

    @property
    def manifest_key(self) -> str:
        return 'eprints'


class IntegrityAll(BaseIntegrity):
    def __init__(self, eprints: IntegrityAllEPrints,
                 listings: IntegrityAllListings,
                 manifest: Optional[Manifest] = None,
                 checksum: Optional[str] = None) -> None:
        super(IntegrityAll, self).__init__(manifest, checksum)
        self.eprints = eprints
        self.listings = listings

    @classmethod
    def make_manifest(cls, eprints: IntegrityAllEPrints,
                     listings: IntegrityAllListings) -> Manifest:
        return Manifest(entries=[
            {'key': eprints.manifest_key, 'checksum': eprints.checksum},
            {'key': listings.manifest_key, 'checksum': listings.checksum}
        ])


def checksum_raw(raw: bytes) -> str:
    hash_md5 = md5()
    hash_md5.update(raw)
    return urlsafe_b64encode(hash_md5.digest()).decode('utf-8')

# def _validate(key: str, content: IO[bytes], manifest: Dict[str, str]) -> None:
#     calculated = checksum(content)
#     if calculated != manifest[key]:
#         raise ChecksumError(f'{key} has non-matching checksum; expected'
#                             f' {manifest[key]}, got {calculated}')

# if validate:    # Compare calculated checksums to the manifest.
#         manifest = json.load(manifest.content)
#         _validate(metadata.key, metadata.content, manifest)
#         _validate(pdf.key, pdf.content, manifest)
#         _validate(source.key, source.content, manifest)




# def _serialize_manifest(version: Version, metadata: MetadataEntry,
#                         source: SourceEntry, render: PDFEntry,
#                         prefix: str) -> ManifestEntry:
#     if version.arxiv_id is None or version.version is None:
#         raise ValueError('Record serialization requires announced e-prints')
#     return ManifestEntry(
#         key=ManifestEntry.make_key(
#             prefix,
#             str(version.arxiv_id),
#             version.version
#         ),
#         content=io.BytesIO(json.dumps({
#             metadata.key: metadata.checksum,
#             source.key: source.checksum,
#             render.key: render.checksum
#         }).encode('utf-8'))
#     )


class ValidationError(Exception):
    """A data consistency problem was encountered."""


class ChecksumError(ValidationError):
    """An unexpected checksum value was encountered."""