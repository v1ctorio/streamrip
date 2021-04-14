"""These classes parse information from Clients into a universal,
downloadable form.
"""

import concurrent.futures
import functools
import logging
import os
import re
import shutil
import subprocess
from tempfile import gettempdir
from typing import Any, Generator, Iterable, Union

import click
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from pathvalidate import sanitize_filename, sanitize_filepath
from tqdm import tqdm

from . import converter
from .clients import Client
from .constants import (
    ALBUM_KEYS,
    FLAC_MAX_BLOCKSIZE,
    FOLDER_FORMAT,
    TRACK_FORMAT,
)
from .db import MusicDB
from .exceptions import (
    InvalidQuality,
    InvalidSourceError,
    NonStreamable,
    TooLargeCoverArt,
)
from .metadata import TrackMetadata
from .utils import (
    clean_format,
    decho,
    decrypt_mqa_file,
    ext,
    safe_get,
    tidal_cover_url,
    tqdm_download,
)

logger = logging.getLogger(__name__)

TYPE_REGEXES = {
    "remaster": re.compile(r"(?i)(re)?master(ed)?"),
    "extra": re.compile(r"(?i)(anniversary|deluxe|live|collector|demo|expanded)"),
}


class Track:
    """Represents a downloadable track.

    Loading metadata as a single track:
    >>> t = Track(client, id='20252078')
    >>> t.load_meta()  # load metadata from api

    Loading metadata as part of an Album:
    >>> t = Track.from_album_meta(api_track_dict, client)

    where `api_track_dict` is a track entry in an album tracklist.

    Downloading and tagging:
    >>> t.download()
    >>> t.tag()
    """

    def __init__(self, client: Client, **kwargs):
        """Create a track object.

        The only required parameter is client, but passing at an id is
        highly recommended. Every value in kwargs will be set as an attribute
        of the object. (TODO: make this safer)

        :param track_id: track id returned by Qobuz API
        :type track_id: Optional[Union[str, int]]
        :param client: qopy client
        :type client: Client
        :param meta: TrackMetadata object
        :type meta: Optional[TrackMetadata]
        :param kwargs: id, filepath_format, meta, quality, folder
        """
        self.client = client
        self.id = None
        self.__dict__.update(kwargs)

        # TODO: remove these
        self.container = "FLAC"
        self.sampling_rate = 44100
        self.bit_depth = 16

        self.downloaded = False
        self.tagged = False
        # TODO: find better solution
        for attr in ("quality", "folder", "meta"):
            setattr(self, attr, None)

        if isinstance(kwargs.get("meta"), TrackMetadata):
            self.meta = kwargs["meta"]

        if (u := kwargs.get("cover_url")) is not None:
            logger.debug(f"Cover url: {u}")
            self.cover_url = u

    def load_meta(self):
        """Send a request to the client to get metadata for this Track."""

        assert self.id is not None, "id must be set before loading metadata"

        self.resp = self.client.get(self.id, media_type="track")
        self.meta = TrackMetadata(
            track=self.resp, source=self.client.source
        )  # meta dict -> TrackMetadata object
        try:
            if self.client.source == "qobuz":
                self.cover_url = self.resp["album"]["image"]["large"]
            elif self.client.source == "tidal":
                self.cover_url = tidal_cover_url(self.resp["album"]["cover"], 320)
            elif self.client.source == "deezer":
                self.cover_url = self.resp["album"]["cover_medium"]
            elif self.client.source == "soundcloud":
                self.cover_url = (
                    self.resp["artwork_url"] or self.resp["user"].get("avatar_url")
                ).replace("large", "t500x500")
            else:
                raise InvalidSourceError(self.client.source)
        except KeyError:
            logger.debug("No cover found")
            self.cover_url = None

    def _prepare_download(self, **kwargs):
        # args override attributes
        self.quality = min(kwargs["quality"], self.client.max_quality)
        self.folder = kwargs["parent_folder"] or self.folder

        self.file_format = kwargs.get("track_format", TRACK_FORMAT)
        self.folder = sanitize_filepath(self.folder, platform="auto")
        self.format_final_path()

        os.makedirs(self.folder, exist_ok=True)

        if self.id in kwargs.get("database", []):
            self.downloaded = True
            self.tagged = True
            self.path = self.final_path
            decho(
                f"{self['title']} already logged in database, skipping.",
                fg="magenta",
            )
            return False  # because the track was not downloaded

        if os.path.isfile(self.final_path):  # track already exists
            self.downloaded = True
            self.tagged = True
            self.path = self.final_path
            decho(f"Track already exists: {self.final_path}", fg="magenta")
            return False

        self.download_cover()  # only downloads for playlists and singles
        self.path = os.path.join(gettempdir(), f"{hash(self.id)}_{self.quality}.tmp")
        return True

    def download(
        self,
        quality: int = 3,
        parent_folder: str = "StreamripDownloads",
        progress_bar: bool = True,
        **kwargs,
    ) -> bool:
        """
        Download the track.

        :param quality: (0, 1, 2, 3, 4)
        :type quality: int
        :param folder: folder to download the files to
        :type folder: Optional[Union[str, os.PathLike]]
        :param progress_bar: turn on/off progress bar
        :type progress_bar: bool
        """
        if not self._prepare_download(
            quality=quality,
            parent_folder=parent_folder,
            progress_bar=progress_bar,
            **kwargs,
        ):
            return False

        if self.client.source == "soundcloud":
            # soundcloud client needs whole dict to get file url
            url_id = self.resp
        else:
            url_id = self.id

        try:
            dl_info = self.client.get_file_url(url_id, self.quality)
        except Exception as e:
            click.secho(f"Unable to download track. {e}", fg="red")
            return False

        if self.client.source == "qobuz":
            if not self.__validate_qobuz_dl_info(dl_info):
                click.secho("Track is not available for download", fg="red")
                return False

            self.sampling_rate = dl_info.get("sampling_rate")
            self.bit_depth = dl_info.get("bit_depth")

        # --------- Download Track ----------
        if self.client.source in ("qobuz", "tidal", "deezer"):
            logger.debug("Downloadable URL found: %s", dl_info.get("url"))
            try:
                tqdm_download(
                    dl_info["url"], self.path, desc=self._progress_desc
                )  # downloads file
            except NonStreamable:
                click.secho(
                    "Track {self!s} is not available for download, skipping.", fg="red"
                )
                return False

        elif self.client.source == "soundcloud":
            self._soundcloud_download(dl_info, self.path)

        else:
            raise InvalidSourceError(self.client.source)

        if (
            self.client.source == "tidal"
            and isinstance(dl_info, dict)
            and dl_info.get("enc_key", False)
        ):
            out_path = f"{self.path}_dec"
            decrypt_mqa_file(self.path, out_path, dl_info["enc_key"])
            self.path = out_path

        if not kwargs.get("stay_temp", False):
            self.move(self.final_path)

        try:
            database = kwargs.get("database")
            database.add(self.id)
            logger.debug(f"{self.id} added to database")
        except AttributeError:  # assume database=None was passed
            pass

        logger.debug("Downloaded: %s -> %s", self.path, self.final_path)

        self.downloaded = True

        if not kwargs.get("keep_cover", True) and hasattr(self, "cover_path"):
            os.remove(self.cover_path)

        return True

    def __validate_qobuz_dl_info(self, info: dict) -> bool:
        return all(
            (info.get("sampling_rate"), info.get("bit_depth"), not info.get("sample"))
        )

    def move(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.move(self.path, path)
        self.path = path

    def _soundcloud_download(self, dl_info: dict) -> str:
        if dl_info["type"] == "mp3":
            self.path += ".mp3"
            # convert hls stream to mp3
            subprocess.call(
                [
                    "ffmpeg",
                    "-i",
                    dl_info["url"],
                    "-c",
                    "copy",
                    "-y",
                    self.path,
                    "-loglevel",
                    "fatal",
                ]
            )
        elif dl_info["type"] == "original":
            tqdm_download(dl_info["url"], self.path, desc=self._progress_desc)

            # if a wav is returned, convert to flac
            engine = converter.FLAC(self.path)
            self.path = f"{self.path}.flac"
            engine.convert(custom_fn=self.path)

            self.final_path = self.final_path.replace(".mp3", ".flac")
            self.quality = 2

    @property
    def _progress_desc(self):
        return click.style(f"Track {int(self.meta.tracknumber):02}", fg="blue")

    def download_cover(self):
        """Downloads the cover art, if cover_url is given."""

        if not hasattr(self, "cover_url"):
            return False

        self.cover_path = os.path.join(gettempdir(), f"cover{hash(self.cover_url)}.jpg")
        logger.debug(f"Downloading cover from {self.cover_url}")
        # click.secho(f"\nDownloading cover art for {self!s}", fg="blue")

        if not os.path.exists(self.cover_path):
            tqdm_download(
                self.cover_url, self.cover_path, desc=click.style("Cover", fg="cyan")
            )
        else:
            logger.debug("Cover already exists, skipping download")

    def format_final_path(self) -> str:
        """Return the final filepath of the downloaded file.

        This uses the `get_formatter` method of TrackMetadata, which returns
        a dict with the keys allowed in formatter strings, and their values in
        the TrackMetadata object.
        """
        formatter = self.meta.get_formatter()
        logger.debug("Track meta formatter %s", formatter)
        filename = clean_format(self.file_format, formatter)
        self.final_path = os.path.join(self.folder, filename)[:250].strip() + ext(
            self.quality, self.client.source
        )

        logger.debug("Formatted path: %s", self.final_path)

        return self.final_path

    @classmethod
    def from_album_meta(cls, album: TrackMetadata, track: dict, client: Client):
        """Return a new Track object initialized with info from the album dicts
        returned by client.get calls.

        :param album: album metadata returned by API
        :param pos: index of the track
        :param client: qopy client object
        :type client: Client
        :raises IndexError
        """

        meta = TrackMetadata(album=album, track=track, source=client.source)
        return cls(client=client, meta=meta, id=track["id"])

    @classmethod
    def from_api(cls, item: dict, client: Client):
        meta = TrackMetadata(track=item, source=client.source)
        try:
            if client.source == "qobuz":
                cover_url = item["album"]["image"]["small"]
            elif client.source == "tidal":
                cover_url = tidal_cover_url(item["album"]["cover"], 320)
            elif client.source == "deezer":
                cover_url = item["album"]["cover_medium"]
            else:
                raise InvalidSourceError(client.source)
        except KeyError:
            logger.debug("No cover found")
            cover_url = None

        return cls(
            client=client,
            meta=meta,
            id=item["id"],
            cover_url=cover_url,
        )

    def tag(
        self,
        album_meta: dict = None,
        cover: Union[Picture, APIC, MP4Cover] = None,
        embed_cover: bool = True,
    ):
        """Tag the track using the stored metadata.

        The info stored in the TrackMetadata object (self.meta) can be updated
        with album metadata if necessary. The cover must be a mutagen cover-type
        object that already has the bytes loaded.

        :param album_meta: album metadata to update Track with
        :type album_meta: dict
        :param cover: initialized mutagen cover object
        :type cover: Union[Picture, APIC]
        :param embed_cover: Embed cover art into file
        :type embed_cover: bool
        """
        assert isinstance(self.meta, TrackMetadata), "meta must be TrackMetadata"
        if not self.downloaded:
            logger.info(
                "Track %s not tagged because it was not downloaded", self["title"]
            )
            return

        if self.tagged:
            logger.info(
                "Track %s not tagged because it is already tagged", self["title"]
            )
            return

        if album_meta is not None:
            self.meta.add_album_meta(album_meta)  # extend meta with album info

        if self.quality in (2, 3, 4):
            self.container = "FLAC"
            logger.debug("Tagging file with %s container", self.container)
            audio = FLAC(self.path)
        elif self.quality <= 1:
            if self.client.source == "tidal":
                self.container = "AAC"
                audio = MP4(self.path)
            else:
                self.container = "MP3"
                try:
                    audio = ID3(self.path)
                except ID3NoHeaderError:
                    audio = ID3()

            logger.debug("Tagging file with %s container", self.container)
        else:
            raise InvalidQuality(f'Invalid quality: "{self.quality}"')

        # automatically generate key, value pairs based on container
        tags = self.meta.tags(self.container)
        for k, v in tags:
            audio[k] = v

        if embed_cover and cover is None:
            assert hasattr(self, "cover_path")
            cover = Tracklist.get_cover_obj(
                self.cover_path, self.quality, self.client.source
            )

        if isinstance(audio, FLAC):
            if embed_cover:
                audio.add_picture(cover)
            audio.save()
        elif isinstance(audio, ID3):
            if embed_cover:
                audio.add(cover)
            audio.save(self.path, "v2_version=3")
        elif isinstance(audio, MP4):
            audio["covr"] = [cover]
            audio.save()
        else:
            raise ValueError(f"Unknown container type: {audio}")

        self.tagged = True

    def convert(self, codec: str = "ALAC", **kwargs):
        """Converts the track to another codec.

        Valid values for codec:
            * FLAC
            * ALAC
            * MP3
            * OPUS
            * OGG
            * VORBIS
            * AAC
            * M4A

        :param codec: the codec to convert the track to
        :type codec: str
        :param kwargs:
        """
        if not self.downloaded:
            logger.debug("Track not downloaded, skipping conversion")
            click.secho("Track not downloaded, skipping conversion", fg="magenta")
            return

        CONV_CLASS = {
            "FLAC": converter.FLAC,
            "ALAC": converter.ALAC,
            "MP3": converter.LAME,
            "OPUS": converter.OPUS,
            "OGG": converter.Vorbis,
            "VORBIS": converter.Vorbis,
            "AAC": converter.AAC,
            "M4A": converter.AAC,
        }

        self.container = codec.upper()
        if not hasattr(self, "final_path"):
            self.format_final_path()

        if not os.path.isfile(self.path):
            logger.info("File %s does not exist. Skipping conversion.", self.path)
            click.secho(f"{self!s} does not exist. Skipping conversion.", fg="red")
            return

        assert (
            self.container in CONV_CLASS
        ), f"Invalid codec {codec}. Must be in {CONV_CLASS.keys()}"

        engine = CONV_CLASS[self.container](
            filename=self.path,
            sampling_rate=kwargs.get("sampling_rate"),
            remove_source=kwargs.get("remove_source", True),
        )
        # click.secho(f"Converting {self!s}", fg="blue")
        engine.convert()
        self.path = engine.final_fn
        self.final_path = self.final_path.replace(
            ext(self.quality, self.client.source), f".{engine.container}"
        )

        if not kwargs.get("stay_temp", False):
            self.move(self.final_path)

    @property
    def title(self) -> str:
        if hasattr(self, "meta"):
            _title = self.meta.title
            if self.meta.explicit:
                _title = f"{_title} (Explicit)"
            return _title
        else:
            raise Exception("Track must be loaded before accessing title")

    def get(self, *keys, default=None) -> Any:
        """Safe get method that allows for layered access.

        :param keys:
        :param default:
        """
        return safe_get(self.meta, *keys, default=default)

    def set(self, key, val):
        """Equivalent to __setitem__. Implemented only for
        consistency.

        :param key:
        :param val:
        """
        self.__setitem__(key, val)

    def __getitem__(self, key: str) -> Any:
        """Dict-like interface for Track metadata.

        :param key:
        """
        return getattr(self.meta, key)

    def __setitem__(self, key: str, val: Any):
        """Dict-like interface for Track metadata.

        :param key:
        :param val:
        """
        setattr(self.meta, key, val)

    def __repr__(self) -> str:
        """Return a string representation of the track.

        :rtype: str
        """
        return f"<Track - {self['title']}>"

    def __str__(self) -> str:
        """Return a readable string representation of
        this track.

        :rtype: str
        """
        return f"{self['artist']} - {self['title']}"


class Tracklist(list):
    """A base class for tracklist-like objects.

    Implements methods to give it dict-like behavior. If a Tracklist
    subclass is subscripted with [s: str], it will return an attribute s.
    If it is subscripted with [i: int] it will return the i'th track in
    the tracklist.
    """

    # anything not in parentheses or brackets
    essence_regex = re.compile(r"([^\(]+)(?:\s*[\(\[][^\)][\)\]])*")

    def download(self, **kwargs):
        self._prepare_download(**kwargs)
        if kwargs.get("conversion", False):
            has_conversion = kwargs["conversion"]["enabled"]
        else:
            has_conversion = False
            kwargs["stay_temp"] = False

        if has_conversion:
            target = self._download_and_convert_item
        else:
            target = self._download_item

        if kwargs.get("concurrent_downloads", True):
            # Tidal errors out with unlimited concurrency
            max_workers = 15 if self.client.source == "tidal" else None
            with concurrent.futures.ThreadPoolExecutor(max_workers) as executor:
                futures = [executor.submit(target, item, **kwargs) for item in self]
                try:
                    concurrent.futures.wait(futures)
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown()
                    tqdm.write("Aborted! May take some time to shutdown.")
                    exit("Aborted!")

        else:
            for item in self:
                click.secho(f'\nDownloading "{item!s}"', fg="blue")
                target(item, **kwargs)

        self.downloaded = True

    def _download_and_convert_item(self, item, **kwargs):
        if self._download_item(item, **kwargs):
            item.convert(**kwargs["conversion"])

    def _download_item(item, **kwargs):
        raise NotImplementedError

    def _prepare_download(**kwargs):
        raise NotImplementedError

    def get(self, key: Union[str, int], default=None):
        if isinstance(key, str):
            if hasattr(self, key):
                return getattr(self, key)

            return default

        if isinstance(key, int):
            if 0 <= key < len(self):
                return self[key]

            return default

    def set(self, key, val):
        self.__setitem__(key, val)

    def convert(self, codec="ALAC", **kwargs):
        if (sr := kwargs.get("sampling_rate")) :
            if sr < 44100:
                logger.warning(
                    "Sampling rate %d is lower than 44.1kHz."
                    "This may cause distortion and ruin the track.",
                    kwargs["sampling_rate"],
                )
            else:
                logger.debug(f"Downsampling to {sr/1000}kHz")

        for track in self:
            track.convert(codec, **kwargs)

    @classmethod
    def from_api(cls, item: dict, client: Client):
        """Create an Album object from the api response of Qobuz, Tidal,
        or Deezer.

        :param resp: response dict
        :type resp: dict
        :param source: in ('qobuz', 'deezer', 'tidal')
        :type source: str
        """
        info = cls._parse_get_resp(item, client=client)

        # equivalent to Album(client=client, **info)
        return cls(client=client, **info)

    @staticmethod
    def get_cover_obj(
        cover_path: str, quality: int, source: str
    ) -> Union[Picture, APIC]:
        """Given the path to an image and a quality id, return an initialized
        cover object that can be used for every track in the album.

        :param cover_path:
        :type cover_path: str
        :param quality:
        :type quality: int
        :rtype: Union[Picture, APIC]
        """

        def flac_mp3_cover_obj(cover):
            cover_obj = cover()
            cover_obj.type = 3
            cover_obj.mime = "image/jpeg"
            with open(cover_path, "rb") as img:
                cover_obj.data = img.read()

            return cover_obj

        if quality > 1:
            cover = Picture
        elif source == "tidal":
            cover = MP4Cover
        else:
            cover = APIC

        if cover is Picture:
            size_ = os.path.getsize(cover_path)
            if size_ > FLAC_MAX_BLOCKSIZE:
                raise TooLargeCoverArt(
                    f"Not suitable for Picture embed: {size_ / 10 ** 6} MB"
                )
            return flac_mp3_cover_obj(cover)

        elif cover is APIC:
            return flac_mp3_cover_obj(cover)

        elif cover is MP4Cover:
            with open(cover_path, "rb") as img:
                return cover(img.read(), imageformat=MP4Cover.FORMAT_JPEG)

        raise InvalidQuality(f"Quality {quality} not allowed")

    def download_message(self):
        click.secho(
            f"\n\nDownloading {self.title} ({self.__class__.__name__})\n",
            fg="blue",
        )

    @staticmethod
    def _parse_get_resp(item, client):
        raise NotImplementedError

    @staticmethod
    def essence(album: str) -> str:
        """Ignore text in parens/brackets, return all lowercase.
        Used to group two albums that may be named similarly, but not exactly
        the same.
        """
        match = Tracklist.essence_regex.match(album)
        if match:
            return match.group(1).strip().lower()

        return album

    def __getitem__(self, key: Union[str, int]):
        if isinstance(key, str):
            return getattr(self, key)

        if isinstance(key, int):
            return super().__getitem__(key)

    def __setitem__(self, key: Union[str, int], val: Any):
        if isinstance(key, str):
            setattr(self, key, val)

        if isinstance(key, int):
            super().__setitem__(key, val)


class Album(Tracklist):
    """Represents a downloadable album.

    Usage:

    >>> resp = client.get('fleetwood mac rumours', 'album')
    >>> album = Album.from_api(resp['items'][0], client)
    >>> album.load_meta()
    >>> album.download()
    """

    def __init__(self, client: Client, **kwargs):
        """Create a new Album object.

        :param client: a qopy client instance
        :param album_id: album id returned by qobuz api
        :type album_id: Union[str, int]
        :param kwargs:
        """
        self.client = client

        self.sampling_rate = None
        self.bit_depth = None
        self.container = None

        # usually an unpacked TrackMetadata obj
        self.__dict__.update(kwargs)

        # to improve from_api method speed
        if kwargs.get("load_on_init", False):
            self.load_meta()

        self.loaded = False
        self.downloaded = False

    def load_meta(self):
        """Load detailed metadata from API using the id."""

        assert hasattr(self, "id"), "id must be set to load metadata"
        resp = self.client.get(self.id, media_type="album")

        # update attributes based on response
        self.meta = self._parse_get_resp(resp, self.client)
        self.__dict__.update(self.meta.asdict())  # used for identification

        if not self.get("streamable", False):
            raise NonStreamable(f"This album is not streamable ({self.id} ID)")

        self._load_tracks(resp)
        self.loaded = True

    @classmethod
    def from_api(cls, resp: dict, client: Client):
        if client.source == "soundcloud":
            return Playlist.from_api(resp, client)

        info = cls._parse_get_resp(resp, client)
        return cls(client, **info)

    def _prepare_download(self, **kwargs):
        self.folder_format = kwargs.get("folder_format", FOLDER_FORMAT)
        self.quality = min(kwargs.get("quality", 3), self.client.max_quality)
        self.folder = self._get_formatted_folder(
            kwargs.get("parent_folder", "StreamripDownloads"), self.quality
        )
        os.makedirs(self.folder, exist_ok=True)

        self.download_message()

        # choose optimal cover size and download it
        click.secho("Downloading cover art", fg="magenta")
        cover_path = os.path.join(gettempdir(), f"cover_{hash(self)}.jpg")
        embed_cover_size = kwargs.get("embed_cover_size", "large")

        assert (
            embed_cover_size in self.cover_urls
        ), f"Invalid cover size. Must be in {self.cover_urls.keys()}"

        embed_cover_url = self.cover_urls[embed_cover_size]
        if embed_cover_url is not None:
            tqdm_download(embed_cover_url, cover_path)
        else:  # sometimes happens with Deezer
            cover_url = functools.reduce(
                lambda c1, c2: c1 or c2, self.cover_urls.values()
            )
            tqdm_download(cover_url, cover_path)

        if kwargs.get("keep_hires_cover", True):
            tqdm_download(
                self.cover_urls["original"], os.path.join(self.folder, "cover.jpg")
            )

        cover_size = os.path.getsize(cover_path)
        if cover_size > FLAC_MAX_BLOCKSIZE:  # 16.77 MB
            click.secho(
                "Downgrading embedded cover size, too large ({cover_size}).",
                fg="bright_yellow",
            )
            # large is about 600x600px which is guaranteed < 16.7 MB
            tqdm_download(self.cover_urls["large"], cover_path)

        embed_cover = kwargs.get("embed_cover", True)  # embed by default
        if self.client.source != "deezer" and embed_cover:
            self.cover_obj = self.get_cover_obj(
                cover_path, self.quality, self.client.source
            )
        else:
            self.cover_obj = None

    def _download_item(
        self,
        track: Track,
        quality: int = 3,
        database: MusicDB = None,
        **kwargs,
    ) -> bool:
        logger.debug("Downloading track to %s", self.folder)
        if self.disctotal > 1:
            disc_folder = os.path.join(self.folder, f"Disc {track.meta.discnumber}")
            kwargs["parent_folder"] = disc_folder
        else:
            kwargs["parent_folder"] = self.folder

        if not track.download(quality=quality, database=database, **kwargs):
            return False

        logger.debug("tagging tracks")
        # deezer tracks come tagged
        if kwargs.get("tag_tracks", True) and self.client.source != "deezer":
            track.tag(cover=self.cover_obj, embed_cover=kwargs.get("embed_cover", True))

        return True

    @staticmethod
    def _parse_get_resp(resp: dict, client: Client) -> dict:
        """Parse information from a client.get(query, 'album') call.

        :param resp:
        :type resp: dict
        :rtype: dict
        """
        meta = TrackMetadata(album=resp, source=client.source)
        meta.id = resp["id"]
        return meta

    def _load_tracks(self, resp):
        """Given an album metadata dict returned by the API, append all of its
        tracks to `self`.

        This uses a classmethod to convert an item into a Track object, which
        stores the metadata inside a TrackMetadata object.
        """
        logging.debug(f"Loading {self.tracktotal} tracks to album")
        for track in _get_tracklist(resp, self.client.source):
            # append method inherited from superclass list
            self.append(
                Track.from_album_meta(
                    album=self.meta,
                    track=track,
                    client=self.client,
                )
            )

    def _get_formatter(self) -> dict:
        fmt = dict()
        for key in ALBUM_KEYS:
            # default to None
            fmt[key] = self.get(key)

        if fmt.get("sampling_rate", False):
            fmt["sampling_rate"] /= 1000
            # change 48.0kHz -> 48kHz, 44.1kHz -> 44.1kHz
            if fmt["sampling_rate"] % 1 == 0.0:
                fmt["sampling_rate"] = int(fmt["sampling_rate"])

        return fmt

    def _get_formatted_folder(self, parent_folder: str, quality: int) -> str:
        if quality >= 2:
            self.container = "FLAC"
        else:
            self.bit_depth = self.sampling_rate = None
            if self.client.source == "tidal":
                self.container = "AAC"
            else:
                self.container = "MP3"

        formatted_folder = clean_format(self.folder_format, self._get_formatter())

        return os.path.join(parent_folder, formatted_folder)

    @property
    def title(self) -> str:
        return self.album

    @title.setter
    def title(self, val: str):
        self.album = val

    def __repr__(self) -> str:
        """Return a string representation of this Album object.

        :rtype: str
        """
        # Avoid AttributeError if load_on_init key is not set
        if hasattr(self, "albumartist"):
            return f"<Album: {self.albumartist} - {self.title}>"

        return f"<Album: V/A - {self.title}>"

    def __str__(self) -> str:
        """Return a readable string representation of
        this album.

        :rtype: str
        """
        return f"{self['albumartist']} - {self['title']}"

    def __len__(self) -> int:
        return self.tracktotal

    def __hash__(self):
        return hash(self.id)


class Playlist(Tracklist):
    """Represents a downloadable playlist.

    Usage:
    >>> resp = client.search('hip hop', 'playlist')
    >>> pl = Playlist.from_api(resp['items'][0], client)
    >>> pl.load_meta()
    >>> pl.download()
    """

    def __init__(self, client: Client, **kwargs):
        """Create a new Playlist object.

        :param client: a qopy client instance
        :param album_id: playlist id returned by qobuz api
        :type album_id: Union[str, int]
        :param kwargs:
        """
        self.client = client

        for k, v in kwargs.items():
            setattr(self, k, v)

        # to improve from_api method speed
        if kwargs.get("load_on_init"):
            self.load_meta()

        self.loaded = False

    @classmethod
    def from_api(cls, resp: dict, client: Client):
        """Return a Playlist object initialized with information from
        a search result returned by the API.

        :param resp: a single search result entry of a playlist
        :type resp: dict
        :param client:
        :type client: Client
        """
        info = cls._parse_get_resp(resp, client)
        return cls(client, **info)

    def load_meta(self, **kwargs):
        """Send a request to fetch the tracklist from the api.

        :param new_tracknumbers: replace the tracknumber with playlist position
        :type new_tracknumbers: bool
        :param kwargs:
        """
        self.meta = self.client.get(self.id, media_type="playlist")
        logger.debug(self.meta)
        self._load_tracks(**kwargs)
        self.loaded = True

    def _load_tracks(self, new_tracknumbers: bool = True):
        """Parses the tracklist returned by the API.

        :param new_tracknumbers: replace tracknumber tag with playlist position
        :type new_tracknumbers: bool
        """
        # TODO: redundant parsing with _parse_get_pres
        if self.client.source == "qobuz":
            self.name = self.meta["name"]
            self.image = self.meta["images"]
            self.creator = safe_get(self.meta, "owner", "name", default="Qobuz")

            tracklist = self.meta["tracks"]["items"]

            def gen_cover(track):
                return track["album"]["image"]["small"]

            def meta_args(track):
                return {"track": track, "album": track["album"]}

        elif self.client.source == "tidal":
            self.name = self.meta["title"]
            self.image = tidal_cover_url(self.meta["image"], 640)
            self.creator = safe_get(self.meta, "creator", "name", default="TIDAL")

            tracklist = self.meta["tracks"]

            def gen_cover(track):
                cover_url = tidal_cover_url(track["album"]["cover"], 640)
                return cover_url

            def meta_args(track):
                return {
                    "track": track,
                    "source": self.client.source,
                }

        elif self.client.source == "deezer":
            self.name = self.meta["title"]
            self.image = self.meta["picture_big"]
            self.creator = safe_get(self.meta, "creator", "name", default="Deezer")

            tracklist = self.meta["tracks"]

            def gen_cover(track):
                return track["album"]["cover_medium"]

        elif self.client.source == "soundcloud":
            self.name = self.meta["title"]
            self.image = self.meta.get("artwork_url").replace("large", "t500x500")
            self.creator = self.meta["user"]["username"]
            tracklist = self.meta["tracks"]

            def gen_cover(track):
                return track["artwork_url"].replace("large", "t500x500")

        else:
            raise NotImplementedError

        self.tracktotal = len(tracklist)
        if self.client.source == "soundcloud":
            # No meta is included in soundcloud playlist
            # response, so it is loaded at download time
            for track in tracklist:
                self.append(Track(self.client, id=track["id"]))
        else:
            for track in tracklist:
                # TODO: This should be managed with .m3u files and alike. Arbitrary
                # tracknumber tags might cause conflicts if the playlist files are
                # inside of a library folder
                meta = TrackMetadata(track=track, source=self.client.source)

                self.append(
                    Track(
                        self.client,
                        id=track.get("id"),
                        meta=meta,
                        cover_url=gen_cover(track),
                    )
                )

        logger.debug(f"Loaded {len(self)} tracks from playlist {self.name}")

    def _prepare_download(self, parent_folder: str = "StreamripDownloads", **kwargs):
        fname = sanitize_filename(self.name)
        self.folder = os.path.join(parent_folder, fname)

        self.__download_index = 1
        self.download_message()

    def _download_item(self, item: Track, **kwargs):
        if self.client.source == "soundcloud":
            item.load_meta()

        if kwargs.get("set_playlist_to_album", False):
            item["album"] = self.name
            item["albumartist"] = self.creator

        if kwargs.get("new_tracknumbers", True):
            item["tracknumber"] = self.__download_index
            item["discnumber"] = 1

            self.__download_index += 1

        self.downloaded = item.download(**kwargs)

        if self.downloaded and self.client.source != "deezer":
            item.tag(embed_cover=kwargs.get("embed_cover", True))

        return self.downloaded

    @staticmethod
    def _parse_get_resp(item: dict, client: Client) -> dict:
        """Parses information from a search result returned
        by a client.search call.

        :param item:
        :type item: dict
        :param client:
        :type client: Client
        """
        if client.source == "qobuz":
            return {
                "name": item["name"],
                "id": item["id"],
            }
        elif client.source == "tidal":
            return {
                "name": item["title"],
                "id": item["uuid"],
            }
        elif client.source == "deezer":
            return {
                "name": item["title"],
                "id": item["id"],
            }
        elif client.source == "soundcloud":
            return {
                "name": item["title"],
                "id": item["permalink_url"],
                "description": item["description"],
                "popularity": f"{item['likes_count']} likes",
                "tracktotal": len(item["tracks"]),
            }

        raise InvalidSourceError(client.source)

    @property
    def title(self) -> str:
        return self.name

    def __repr__(self) -> str:
        """Return a string representation of this Playlist object.

        :rtype: str
        """
        return f"<Playlist: {self.name}>"

    def __str__(self) -> str:
        """Return a readable string representation of
        this track.

        :rtype: str
        """
        return f"{self.name} ({len(self)} tracks)"


class Artist(Tracklist):
    """Represents a downloadable artist.

    Usage:
    >>> resp = client.get('fleetwood mac', 'artist')
    >>> artist = Artist.from_api(resp['items'][0], client)
    >>> artist.load_meta()
    >>> artist.download()
    """

    def __init__(self, client: Client, **kwargs):
        """Create a new Artist object.

        :param client: a qopy client instance
        :param album_id: artist id returned by qobuz api
        :type album_id: Union[str, int]
        :param kwargs:
        """
        self.client = client

        for k, v in kwargs.items():
            setattr(self, k, v)

        # to improve from_api method speed
        if kwargs.get("load_on_init"):
            self.load_meta()

        self.loaded = False

    def load_meta(self):
        """Send an API call to get album info based on id."""
        self.meta = self.client.get(self.id, media_type="artist")
        self._load_albums()
        self.loaded = True

    # override
    def download(self, **kwargs):
        iterator = self._prepare_download(**kwargs)
        for item in iterator:
            self._download_item(item, **kwargs)

    def _load_albums(self):
        """From the discography returned by client.get(query, 'artist'),
        generate album objects and append them to self.
        """
        if self.client.source == "qobuz":
            self.name = self.meta["name"]
            albums = self.meta["albums"]["items"]

        elif self.client.source == "tidal":
            self.name = self.meta["name"]
            albums = self.meta["albums"]

        elif self.client.source == "deezer":
            # TODO: load artist name
            albums = self.meta["albums"]

        else:
            raise InvalidSourceError(self.client.source)

        for album in albums:
            logger.debug("Appending album: %s", album.get("title"))
            self.append(Album.from_api(album, self.client))

    def _prepare_download(
        self, parent_folder: str = "StreamripDownloads", filters: tuple = (), **kwargs
    ) -> Iterable:
        folder = sanitize_filename(self.name)
        folder = os.path.join(parent_folder, folder)

        logger.debug("Artist folder: %s", folder)
        logger.debug(f"Length of tracklist {len(self)}")
        logger.debug(f"Filters: {filters}")

        if "repeats" in filters:
            final = self._remove_repeats(bit_depth=max, sampling_rate=min)
            filters = tuple(f for f in filters if f != "repeats")
        else:
            final = self

        if isinstance(filters, tuple) and self.client.source == "qobuz":
            filter_funcs = (getattr(self, f"_{filter_}") for filter_ in filters)
            for func in filter_funcs:
                final = filter(func, final)

        self.download_message()
        return final

    def _download_item(
        self,
        item,
        parent_folder: str = "StreamripDownloads",
        quality: int = 3,
        database: MusicDB = None,
        **kwargs,
    ) -> bool:
        try:
            item.load_meta()
        except NonStreamable:
            logger.info("Skipping album, not available to stream.")
            return

        # always an Album
        status = item.download(
            parent_folder=parent_folder,
            quality=quality,
            database=database,
            **kwargs,
        )
        return status

    @property
    def title(self) -> str:
        return self.name

    @classmethod
    def from_api(cls, item: dict, client: Client, source: str = "qobuz"):
        """Create an Artist object from the api response of Qobuz, Tidal,
        or Deezer.

        :param resp: response dict
        :type resp: dict
        :param source: in ('qobuz', 'deezer', 'tidal')
        :type source: str
        """
        logging.debug("Loading item from API")
        info = cls._parse_get_resp(item, client)

        # equivalent to Artist(client=client, **info)
        return cls(client=client, **info)

    @staticmethod
    def _parse_get_resp(item: dict, client: Client) -> dict:
        """Parse a result from a client.search call.

        :param item: the item to parse
        :type item: dict
        :param client:
        :type client: Client
        """
        if client.source in ("qobuz", "deezer"):
            info = {
                "name": item.get("name"),
                "id": item.get("id"),
            }
        elif client.source == "tidal":
            info = {
                "name": item["name"],
                "id": item["id"],
            }
        else:
            raise InvalidSourceError(client.source)

        return info

    # ----------- Filters --------------

    def _remove_repeats(self, bit_depth=max, sampling_rate=max) -> Generator:
        """Remove the repeated albums from self. May remove different
        versions of the same album.

        :param bit_depth: either max or min functions
        :param sampling_rate: either max or min functions
        """
        groups = dict()
        for album in self:
            if (t := self.essence(album.title)) not in groups:
                groups[t] = []
            groups[t].append(album)

        for group in groups.values():
            assert bit_depth in (min, max) and sampling_rate in (min, max)
            best_bd = bit_depth(a["bit_depth"] for a in group)
            best_sr = sampling_rate(a["sampling_rate"] for a in group)
            for album in group:
                if album["bit_depth"] == best_bd and album["sampling_rate"] == best_sr:
                    yield album
                    break

    def _non_studio_albums(self, album: Album) -> bool:
        """Passed as a parameter by the user.

        This will download only studio albums.

        :param artist: usually self
        :param album: the album to check
        :type album: Album
        :rtype: bool
        """
        return (
            album["albumartist"] != "Various Artists"
            and TYPE_REGEXES["extra"].search(album.title) is None
        )

    def _features(self, album: Album) -> bool:
        """Passed as a parameter by the user.

        This will download only albums where the requested
        artist is the album artist.

        :param artist: usually self
        :param album: the album to check
        :type album: Album
        :rtype: bool
        """
        return self["name"] == album["albumartist"]

    def _extras(self, album: Album) -> bool:
        """Passed as a parameter by the user.

        This will skip any extras.

        :param artist: usually self
        :param album: the album to check
        :type album: Album
        :rtype: bool
        """
        return TYPE_REGEXES["extra"].search(album.title) is None

    def _non_remasters(self, album: Album) -> bool:
        """Passed as a parameter by the user.

        This will download only remasterd albums.

        :param artist: usually self
        :param album: the album to check
        :type album: Album
        :rtype: bool
        """
        return TYPE_REGEXES["remaster"].search(album.title) is not None

    def _non_albums(self, album: Album) -> bool:
        """This will ignore non-album releases.

        :param artist: usually self
        :param album: the album to check
        :type album: Album
        :rtype: bool
        """
        # Doesn't work yet
        return album["release_type"] == "album"

    # --------- Magic Methods --------

    def __repr__(self) -> str:
        """Return a string representation of this Artist object.

        :rtype: str
        """
        return f"<Artist: {self.name}>"

    def __str__(self) -> str:
        """Return a readable string representation of
        this Artist.

        :rtype: str
        """
        return self.name

    def __hash__(self) -> int:
        return hash(self.id)


class Label(Artist):
    def load_meta(self):
        assert self.client.source == "qobuz", "Label source must be qobuz"

        resp = self.client.get(self.id, "label")
        self.name = resp["name"]
        for album in resp["albums"]["items"]:
            self.append(Album.from_api(album, client=self.client))

        self.loaded = True

    def __repr__(self):
        return f"<Label - {self.name}>"

    def __str__(self) -> str:
        """Return a readable string representation of
        this track.

        :rtype: str
        """
        return self.name


def _get_tracklist(resp, source) -> list:
    if source == "qobuz":
        return resp["tracks"]["items"]
    if source in ("tidal", "deezer"):
        return resp["tracks"]

    raise NotImplementedError(source)
