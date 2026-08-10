#!/usr/bin/env python3
"""Adapter that lets spotify_to_tidal read Spotify playlists via the
`spotifyscraper` library instead of the official Spotify Web API.

This is useful when the account behind the Spotify app no longer has an active
premium subscription (the Web API then returns HTTP 403 for playlist reads).
The scraper fetches public playlist data without any API keys.

Only the *reading of the Spotify playlist* is replaced here; the Tidal matching
and syncing logic in ``sync.py`` is left completely unchanged. To achieve that,
``playlist()`` returns a dict shaped like the one ``spotipy`` produces, with the
already-converted tracks embedded under the ``_scraper_tracks`` key so that
``sync.get_tracks_from_spotify_playlist`` can consume them directly.

Caveats stemming from the data the scraper exposes:
  * No ISRC is available, so ``isrc_match`` never fires and matching falls back
    to the existing duration + name + artist heuristic.
  * Album artists are not exposed, so we fill them heuristically from the
    track's primary artist to keep the album-search code path working.
"""

import html
import sys

from spotify_scraper import SpotifyClient

__all__ = [
    'open_spotify_scraper_session',
    'SpotifyScraperSession',
]


def _clean(text: str | None) -> str:
    # scraper values may contain HTML entities (e.g. "&#x2F;", "&amp;")
    return html.unescape(text) if text else ""


def extract_playlist_id(uri: str) -> str:
    """Accept a raw id, a ``spotify:playlist:<id>`` uri, or an open.spotify.com url."""
    value = uri.strip()
    if "playlist:" in value:
        return value.rsplit(":", 1)[-1]
    if "/playlist/" in value:
        tail = value.split("/playlist/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0]
    return value


def _convert_track(playlist_track) -> dict:
    """Convert a scraper ``PlaylistTrack`` into a spotipy-shaped SpotifyTrack dict.

    Only the fields consumed by ``sync.py`` are populated.
    """
    track = playlist_track.track
    artists = [{'name': _clean(a.name)} for a in track.artists]
    if track.album is not None:
        # AlbumRef exposes no artists; reuse the track artists as a best-effort
        # so album-based Tidal search can still run. Compilations may not match
        # this way and simply fall through to the standalone-track search.
        album = {'name': _clean(track.album.name), 'artists': list(artists)}
    else:
        album = {'name': "", 'artists': []}

    return {
        'id': track.id,
        'name': _clean(track.name),
        'duration_ms': track.duration_ms,
        'track_number': track.track_number or 1,
        'type': 'track',
        'external_ids': {},  # ISRC not available from the scraper
        'artists': artists,
        'album': album,
    }


class SpotifyScraperSession:
    """Minimal stand-in for a ``spotipy.Spotify`` session, scraper-backed.

    Supports the single operation needed by the ``--uri`` sync path:
    fetching one playlist and its full track list.
    """

    def __init__(self, client: SpotifyClient):
        self._client = client

    def playlist(self, playlist_id: str) -> dict:
        playlist_id = extract_playlist_id(playlist_id)
        # max_tracks=None fetches every track (the default caps at 100)
        pl = self._client.get_playlist(playlist_id, max_tracks=None)
        tracks = [_convert_track(pt) for pt in pl.tracks]
        return {
            'id': pl.id,
            'uri': pl.uri,
            'name': _clean(pl.name),
            'description': _clean(pl.description),
            '_scraper_tracks': tracks,
        }

    def close(self) -> None:
        self._client.close()

    # Any spotipy method the scraper cannot serve (favorites, followed artists,
    # saved albums, listing the logged-in user's own playlists) surfaces here
    # with a clear message instead of a confusing AttributeError.
    def __getattr__(self, name: str):
        def _unsupported(*_args, **_kwargs):
            sys.exit(
                f"'{name}' is not available in spotify-scraper mode. "
                "Only public playlist sync via --uri (or sync_playlists in the "
                "config) is supported without a Spotify API subscription."
            )
        return _unsupported


def open_spotify_scraper_session() -> SpotifyScraperSession:
    return SpotifyScraperSession(SpotifyClient())
