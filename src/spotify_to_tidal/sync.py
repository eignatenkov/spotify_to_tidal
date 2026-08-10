#!/usr/bin/env python3

import asyncio
from .cache import failure_cache, track_match_cache
import datetime
from difflib import SequenceMatcher
from functools import partial
from typing import Callable, List, Sequence, Set, Mapping
import math
import re
import requests
import sys
import spotipy
import tidalapi
from tidalapi.exceptions import InvalidISRC, ObjectNotFound
from .tidalapi_patch import add_multiple_tracks_to_playlist, clear_tidal_playlist, remove_indices_from_playlist, get_all_favorites, get_all_playlists, get_all_playlist_tracks
import time
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm
import traceback
import unicodedata
import math

from .type import spotify as t_spotify

def normalize(s) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')

def simple(input_string: str) -> str:
    # only take the first part of a string before any hyphens or brackets to account for different versions
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()

# Apostrophe variants (straight, curly, modifier-letter, acute, grave) are dropped
# entirely so that "Paul's", "Paul’s" and "Paul´s" all collapse to "pauls".
_APOSTROPHES = dict.fromkeys(map(ord, "'’‘ʼ´`"), None)
# Splits a credit string into individual artists. Only commas, ampersands and an
# explicit "feat."/"featuring" marker are treated as separators — deliberately NOT
# " and "/" with "/"/" ", which appear inside real single-act names ("Iron and Wine",
# "Belle and Sebastian", "AC/DC"). "feat" is word-boundary-anchored so it doesn't fire
# inside words like "Defeated".
_ARTIST_SPLIT = re.compile(r'[,&]|\bfeaturing\b|\bfeat\b\.?', re.IGNORECASE)
# A trailing ensemble-size qualifier that Spotify and Tidal disagree on ("Lewis" vs
# "Lewis Quartet"). Anchored to the end of the cleaned string so it only strips a
# genuine trailing qualifier, never an occurrence mid-name.
_BAND_SUFFIX = re.compile(r'\s+(?:quartet|quintet|trio|duo|sextet|septet|octet|band|ensemble|orchestra|group|project|collective)$')

def clean(s: str) -> str:
    """Aggressively normalize a string for fuzzy comparison: fold ligatures and
    accented characters via NFKD (so "ﬀ" -> "ff", "ü" -> "u"), drop apostrophes and
    combining marks, lower-case, and collapse every run of non-alphanumeric
    characters to a single space. Unlike ``normalize`` (NFD), this folds
    compatibility characters, which is what makes ligatures/curly-quotes comparable."""
    if not s:
        return ""
    s = s.translate(_APOSTROPHES)
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()

def simple_name(s: str) -> str:
    """The comparable core of a track title: drop a ' - ' version suffix, any
    parenthetical/bracketed suffix, and a trailing 'feat.' credit, then clean().
    Uses ' - ' (spaced) rather than a bare '-' so hyphenated words survive."""
    s = s.split(' - ')[0]
    s = re.split(r'[\(\[]', s)[0]
    s = re.split(r'\bfeaturing\b|\bfeat\b\.?', s, flags=re.IGNORECASE)[0]
    return clean(s)

def validate_and_format_isrc(isrc: str) -> str | None:
    if not isrc or not isinstance(isrc, str):
        return None

    clean_isrc = isrc.replace('-', '').upper().strip()
    
    if len(clean_isrc) != 12:
        return None
    
    if not re.match(r'^[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}$', clean_isrc):
        return None
    
    formatted_isrc = f"{clean_isrc[:2]}-{clean_isrc[2:5]}-{clean_isrc[5:7]}-{clean_isrc[7:12]}"
    return formatted_isrc

def isrc_match(tidal_track: tidalapi.Track, spotify_track) -> bool:
    if "isrc" in spotify_track["external_ids"]:
        return tidal_track.isrc == spotify_track["external_ids"]["isrc"]
    return False

def duration_match(tidal_track: tidalapi.Track, spotify_track, tolerance=2) -> bool:
    # the duration of the two tracks must be the same to within 2 seconds
    return abs(tidal_track.duration - spotify_track['duration_ms']/1000) < tolerance

_NAME_EXCLUSIONS = ("instrumental", "acapella", "remix")

def name_match(tidal_track, spotify_track) -> bool:
    # A distinguishing keyword must be present on both sides or neither, so a track
    # never matches its own instrumental/acapella/remix. Checked on the raw (lower)
    # strings, including the Tidal version field, before any cleaning strips them.
    spotify_name = spotify_track['name']
    spotify_blob = spotify_name.lower()
    tidal_blob = tidal_track.name.lower()
    if tidal_track.version:
        tidal_blob += " " + tidal_track.version.lower()
    for pattern in _NAME_EXCLUSIONS:
        if (pattern in spotify_blob) != (pattern in tidal_blob):
            return False

    # Compare the cleaned core of the Spotify title against the cleaned full Tidal
    # title. Accept a substring match either way (handles "Part II" vs "Adjust: Part
    # II" and vice-versa), otherwise fall back to a high fuzzy ratio to tolerate
    # source-metadata typos ("Sereande" vs "Serenade"). The surrounding match() still
    # requires duration and artist agreement, which guards short-substring collisions.
    spotify_core = simple_name(spotify_name)
    tidal_full = clean(tidal_track.name)
    if not spotify_core:
        # The title was entirely a version/parenthetical suffix (e.g. "(Interlude)").
        # Fall back to the uncleaned-of-suffixes full name rather than matching blindly.
        spotify_core = clean(spotify_name)
    if not spotify_core:
        return True
    if spotify_core in tidal_full or tidal_full in spotify_core:
        return True
    return SequenceMatcher(None, spotify_core, tidal_full).ratio() >= 0.86

def _artist_name_set(names) -> Set[str]:
    """Split each credit into individual artists, clean them, and drop a trailing
    ensemble-size qualifier so "James Brandon Lewis Quartet" reduces to
    "james brandon lewis" (matching a Spotify credit of just "James Brandon Lewis")."""
    result: Set[str] = set()
    for name in names:
        for part in _ARTIST_SPLIT.split(name):
            token = _BAND_SUFFIX.sub('', clean(part)).strip()
            if token:
                result.add(token)
    return result

def artist_match(tidal: tidalapi.Track | tidalapi.Album, spotify) -> bool:
    # There must be at least one overlapping artist (after normalization and trailing
    # ensemble-suffix stripping) between the Tidal and Spotify track. A stricter
    # exact-set intersection is used deliberately: an earlier word-subset containment
    # fallback let a single stray token (e.g. "the" left over from "The Trio") match
    # unrelated artists, so it was removed.
    tidal_artists = _artist_name_set(artist.name for artist in tidal.artists)
    spotify_artists = _artist_name_set(artist['name'] for artist in spotify['artists'])
    return bool(tidal_artists & spotify_artists)

def match(tidal_track, spotify_track) -> bool:
    if not spotify_track['id']: return False
    return isrc_match(tidal_track, spotify_track) or (
        duration_match(tidal_track, spotify_track)
        and name_match(tidal_track, spotify_track)
        and artist_match(tidal_track, spotify_track)
    )

def test_album_similarity(spotify_album, tidal_album, threshold=0.6):
    return SequenceMatcher(None, simple(spotify_album['name']), simple(tidal_album.name)).ratio() >= threshold and artist_match(tidal_album, spotify_album)

async def tidal_search(spotify_track, rate_limiter, tidal_session: tidalapi.Session) -> tidalapi.Track | None:
    def _search_for_track_in_album():
        # search for album name and first album artist
        if 'album' in spotify_track and 'artists' in spotify_track['album'] and len(spotify_track['album']['artists']):
            query = simple(spotify_track['album']['name']) + " " + simple(spotify_track['album']['artists'][0]['name'])
            album_result = tidal_session.search(query, models=[tidalapi.album.Album])
            for album in album_result['albums']:
                if album.num_tracks >= spotify_track['track_number'] and test_album_similarity(spotify_track['album'], album):
                    album_tracks = album.tracks()
                    if len(album_tracks) < spotify_track['track_number']:
                        assert( not len(album_tracks) == album.num_tracks ) # incorrect metadata :(
                        continue
                    track = album_tracks[spotify_track['track_number'] - 1]
                    if match(track, spotify_track):
                        failure_cache.remove_match_failure(spotify_track['id'])
                        return track

    def _search_for_standalone_track():
        # if album search fails then search for track name and first artist
        query = simple(spotify_track['name']) + ' ' + simple(spotify_track['artists'][0]['name'])
        for track in tidal_session.search(query, models=[tidalapi.media.Track])['tracks']:
            if match(track, spotify_track):
                failure_cache.remove_match_failure(spotify_track['id'])
                return track
    await rate_limiter.acquire()
    album_search = await asyncio.to_thread( _search_for_track_in_album )
    if album_search:
        return album_search
    await rate_limiter.acquire()
    track_search = await asyncio.to_thread( _search_for_standalone_track )
    if track_search:
        return track_search

    # if none of the search modes succeeded then store the track id to the failure cache
    failure_cache.cache_match_failure(spotify_track['id'])

async def repeat_on_request_error(function, *args, remaining=5, **kwargs):
    # utility to repeat calling the function up to 5 times if an exception is thrown
    try:
        return await function(*args, **kwargs)
    except (tidalapi.exceptions.TooManyRequests, requests.exceptions.RequestException, spotipy.exceptions.SpotifyException) as e:
        if remaining:
            print(f"{str(e)} occurred, retrying {remaining} times")
        else:
            print(f"{str(e)} could not be recovered")

        if isinstance(e, requests.exceptions.RequestException) and not e.response is None:
            print(f"Response message: {e.response.text}")
            print(f"Response headers: {e.response.headers}")

        if not remaining:
            print("Aborting sync")
            print(f"The following arguments were provided:\n\n {str(args)}")
            print(traceback.format_exc())
            sys.exit(1)
        sleep_schedule = {5: 1, 4:10, 3:60, 2:5*60, 1:10*60} # sleep variable length of time depending on retry number
        time.sleep(sleep_schedule.get(remaining, 1))
        return await repeat_on_request_error(function, *args, remaining=remaining-1, **kwargs)


async def _fetch_all_from_spotify_in_chunks(fetch_function: Callable, item_key: str = "track") -> List[dict]:
    output = []
    results = fetch_function(0)
    
    # Get all the items from the first chunk
    output.extend([item[item_key] for item in results['items'] if item.get(item_key) is not None])

    # Get all the remaining chunks in parallel
    if results['next']:
        offsets = [results['limit'] * n for n in range(1, math.ceil(results['total'] / results['limit']))]
        extra_results = await atqdm.gather(
            *[asyncio.to_thread(fetch_function, offset) for offset in offsets],
            desc="Fetching additional data chunks"
        )
        for extra_result in extra_results:
            output.extend([item[item_key] for item in extra_result['items'] if item.get(item_key) is not None])

    return output


async def get_tracks_from_spotify_playlist(spotify_session: spotipy.Spotify, spotify_playlist):
    def _get_tracks_from_spotify_playlist(offset: int, playlist_id: str):
        fields = "next,total,limit,items(track(name,album(name,artists),artists,track_number,duration_ms,id,external_ids(isrc))),type"
        return spotify_session.playlist_tracks(playlist_id=playlist_id, fields=fields, offset=offset)

    print(f"Loading tracks from Spotify playlist '{spotify_playlist['name']}'")
    if '_scraper_tracks' in spotify_playlist:
        # Playlist was fetched via spotify-scraper; tracks are already converted
        # and fully loaded, so no additional paging against the API is needed.
        items = spotify_playlist['_scraper_tracks']
    else:
        items = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, lambda offset: _get_tracks_from_spotify_playlist(offset=offset, playlist_id=spotify_playlist["id"]))
    track_filter = lambda item: item.get('type', 'track') == 'track' # type may be 'episode' also
    # A track only needs its own name and at least one artist to be matchable; the album
    # (and album artists) are optional, since album-based search self-guards on them.
    # Previously this gated on album['artists'], which silently dropped EVERY track when
    # spotify-scraper degraded to embed ("tier-2") data (album is None there) — turning a
    # degraded fetch into an invisible no-op. See spotify_scraper_adapter._convert_track.
    sanity_filter = lambda item: bool(item.get('name')) and bool(item.get('artists'))
    tracks = list(filter(track_filter, items))
    usable = list(filter(sanity_filter, tracks))
    dropped = len(tracks) - len(usable)
    if dropped:
        print(f"Warning: skipped {dropped}/{len(tracks)} track(s) in '{spotify_playlist['name']}' "
              f"missing name/artist metadata (possible degraded Spotify fetch)")
    return usable

def populate_track_match_cache(spotify_tracks_: Sequence[t_spotify.SpotifyTrack], tidal_tracks_: Sequence[tidalapi.Track]):
    """ Populate the track match cache with all the existing tracks in Tidal playlist corresponding to Spotify playlist """
    def _populate_one_track_from_spotify(spotify_track: t_spotify.SpotifyTrack):
        for idx, tidal_track in list(enumerate(tidal_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_track['id'], tidal_track.id))
                tidal_tracks.pop(idx)
                return

    def _populate_one_track_from_tidal(tidal_track: tidalapi.Track):
        for idx, spotify_track in list(enumerate(spotify_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_track['id'], tidal_track.id))
                spotify_tracks.pop(idx)
                return

    # make a copy of the tracks to avoid modifying original arrays
    spotify_tracks = [t for t in spotify_tracks_]
    tidal_tracks = [t for t in tidal_tracks_]

    # first populate from the tidal tracks
    for track in tidal_tracks:
        _populate_one_track_from_tidal(track)
    # then populate from the subset of Spotify tracks that didn't match (to account for many-to-one style mappings)
    for track in spotify_tracks:
        _populate_one_track_from_spotify(track)

def get_new_spotify_tracks(spotify_tracks: Sequence[t_spotify.SpotifyTrack]) -> List[t_spotify.SpotifyTrack]:
    ''' Extracts only the tracks that have not already been seen in our Tidal caches '''
    results = []
    for spotify_track in spotify_tracks:
        if not spotify_track['id']: continue
        if not track_match_cache.get(spotify_track['id']) and not failure_cache.has_match_failure(spotify_track['id']):
            results.append(spotify_track)
    return results

def get_tracks_for_new_tidal_playlist(spotify_tracks: Sequence[t_spotify.SpotifyTrack]) -> Sequence[int]:
    ''' gets list of corresponding tidal track ids for each spotify track, ignoring duplicates '''
    output = []
    seen_tracks = set()

    for spotify_track in spotify_tracks:
        if not spotify_track['id']: continue
        tidal_id = track_match_cache.get(spotify_track['id'])
        if tidal_id:
            if tidal_id in seen_tracks:
                track_name = spotify_track['name']
                artist_names = ', '.join([artist['name'] for artist in spotify_track['artists']])
                print(f'Duplicate found: Track "{track_name}" by {artist_names} will be ignored') 
            else:
                output.append(tidal_id)
                seen_tracks.add(tidal_id)
    return output

async def search_new_tracks_on_tidal(tidal_session: tidalapi.Session, spotify_tracks: Sequence[t_spotify.SpotifyTrack], playlist_name: str, config: dict):
    """ Generic function for searching for each item in a list of Spotify tracks which have not already been seen and adding them to the cache """
    async def _run_rate_limiter(semaphore):
        ''' Leaky bucket algorithm for rate limiting. Periodically releases items from semaphore at rate_limit'''
        _sleep_time = config.get('max_concurrency', 10)/config.get('rate_limit', 10)/4 # aim to sleep approx time to drain 1/4 of 'bucket'
        t0 = datetime.datetime.now()
        while True:
            await asyncio.sleep(_sleep_time)
            t = datetime.datetime.now()
            dt = (t - t0).total_seconds()
            new_items = round(config.get('rate_limit', 10)*dt)
            t0 = t
            [semaphore.release() for i in range(new_items)] # leak new_items from the 'bucket'

    # Extract the new tracks that do not already exist in the old tidal tracklist
    tracks_to_search = get_new_spotify_tracks(spotify_tracks)
    if not tracks_to_search:
        return

    # Search for each of the tracks on Tidal concurrently
    task_description = "Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(tracks_to_search), len(spotify_tracks), playlist_name)
    semaphore = asyncio.Semaphore(config.get('max_concurrency', 10))
    rate_limiter_task = asyncio.create_task(_run_rate_limiter(semaphore))
    search_results = await atqdm.gather( *[ repeat_on_request_error(tidal_search, t, semaphore, tidal_session) for t in tracks_to_search ], desc=task_description )
    rate_limiter_task.cancel()

    # Add the search results to the cache
    song404 = []
    for idx, spotify_track in enumerate(tracks_to_search):
        if search_results[idx]:
            track_match_cache.insert( (spotify_track['id'], search_results[idx].id) )
        else:
            song404.append(f"{spotify_track['id']}: {','.join([a['name'] for a in spotify_track['artists']])} - {spotify_track['name']}")
            color = ('\033[91m', '\033[0m')
            print(color[0] + "Could not find the track " + song404[-1] + color[1])
    file_name = "songs not found.txt"
    with open(file_name, "a", encoding="utf-8") as file:
        for song in song404:
            file.write(f"{song}\n")

def album_match(tidal_album, spotify_album, threshold=0.6):
    """Check if the Spotify album is similar to the Tidal album."""
    name_match = SequenceMatcher(None, simple(spotify_album['name']), simple(tidal_album.name)).ratio() >= threshold
    artist_match = any(
        normalize(artist.name.lower()) == normalize(spotify_album['artists'][0]['name'].lower())
        for artist in tidal_album.artists
    )
    track_count_match = tidal_album.num_tracks == spotify_album.get("total_tracks", -1)
    return name_match and artist_match and track_count_match

def reconcile_tidal_playlist(tidal_playlist: tidalapi.UserPlaylist, old_tidal_track_ids: Sequence[int], new_tidal_track_ids: Sequence[int]):
    """ Bring the Tidal playlist in line with the desired track list by removing only the tracks
        that are no longer wanted and appending only the genuinely new ones, WITHOUT clearing and
        rebuilding the whole playlist. This is order-insensitive: tracks that stay keep their
        existing position (and their Tidal date-added), and new tracks are appended at the end, so
        the order won't necessarily match Spotify exactly. That trade-off is deliberate — it avoids
        wiping the playlist (and resetting date-added/order) on every mid-list insert, removal or
        reorder. """
    new_id_set = set(new_tidal_track_ids)
    old_id_set = set(old_tidal_track_ids)

    # Indices (in the current playlist) to delete: any track not in the desired set, plus duplicate
    # occurrences of a wanted track beyond its first appearance.
    indices_to_remove = []
    kept = set()
    for idx, tid in enumerate(old_tidal_track_ids):
        if tid in new_id_set and tid not in kept:
            kept.add(tid)
        else:
            indices_to_remove.append(idx)

    # Desired tracks not already present (new_tidal_track_ids is already de-duplicated upstream).
    ids_to_add = [tid for tid in new_tidal_track_ids if tid not in old_id_set]

    print(f"Reconciling Tidal playlist in place: removing {len(indices_to_remove)}, adding {len(ids_to_add)} (keeping {len(kept)})")
    if indices_to_remove:
        remove_indices_from_playlist(tidal_playlist, indices_to_remove)
    if ids_to_add:
        add_multiple_tracks_to_playlist(tidal_playlist, ids_to_add)

async def sync_playlist(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, spotify_playlist, tidal_playlist: tidalapi.Playlist | None, config: dict):
    """ sync given playlist to tidal """
    # Get the tracks from both Spotify and Tidal, creating a new Tidal playlist if necessary
    spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)
    if len(spotify_tracks) == 0:
        # Make a degraded/empty fetch visible instead of looking like "already in sync":
        # leaving the Tidal playlist untouched here is only correct if the source really is empty.
        print(f"No usable tracks fetched for Spotify playlist '{spotify_playlist['name']}' — "
              f"leaving Tidal playlist unchanged (source empty or fetch degraded)")
        return
    if tidal_playlist:
        old_tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    else:
        print(f"No playlist found on Tidal corresponding to Spotify playlist: '{spotify_playlist['name']}', creating new playlist")
        tidal_playlist =  tidal_session.user.create_playlist(spotify_playlist['name'], spotify_playlist['description'])
        old_tidal_tracks = []

    # Extract the new tracks from the playlist that we haven't already seen before
    populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    await search_new_tracks_on_tidal(tidal_session, spotify_tracks, spotify_playlist['name'], config)
    new_tidal_track_ids = get_tracks_for_new_tidal_playlist(spotify_tracks)

    # Update the Tidal playlist if there are changes
    old_tidal_track_ids = [t.id for t in old_tidal_tracks]
    if new_tidal_track_ids == old_tidal_track_ids:
        print("No changes to write to Tidal playlist")
    elif new_tidal_track_ids[:len(old_tidal_track_ids)] == old_tidal_track_ids:
        # Fast path: the change is a pure end-append and the existing order already matches, so
        # just append the new tail (preserves order exactly).
        add_multiple_tracks_to_playlist(tidal_playlist, new_tidal_track_ids[len(old_tidal_track_ids):])
    else:
        # Any other change (mid-list insert/removal/reorder, or a track that dropped out): reconcile
        # in place by removing only what's gone and adding only what's new, instead of wiping and
        # rebuilding the whole playlist.
        reconcile_tidal_playlist(tidal_playlist, old_tidal_track_ids, new_tidal_track_ids)

async def sync_favorites(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    """ sync user favorites to tidal """
    async def get_tracks_from_spotify_favorites() -> List[dict]:
        _get_favorite_tracks = lambda offset: spotify_session.current_user_saved_tracks(offset=offset)    
        tracks = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, _get_favorite_tracks)
        tracks.reverse()
        return tracks

    def get_new_tidal_favorites() -> List[int]:
        existing_favorite_ids = set([track.id for track in old_tidal_tracks])
        new_ids = []
        for spotify_track in spotify_tracks:
            match_id = track_match_cache.get(spotify_track['id'])
            if match_id and not match_id in existing_favorite_ids:
                new_ids.append(match_id)
        return new_ids

    print("Loading favorite tracks from Spotify")
    spotify_tracks = await get_tracks_from_spotify_favorites()
    print("Loading existing favorite tracks from Tidal")
    old_tidal_tracks = await get_all_favorites(tidal_session.user.favorites, order='DATE')
    populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    await search_new_tracks_on_tidal(tidal_session, spotify_tracks, "Favorites", config)
    new_tidal_favorite_ids = get_new_tidal_favorites()
    if new_tidal_favorite_ids:
        for tidal_id in tqdm(new_tidal_favorite_ids, desc="Adding new tracks to Tidal favorites"):
            tidal_session.user.favorites.add_track(tidal_id)
    else:
        print("No new tracks to add to Tidal favorites")

async def sync_artists(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    """Synchronize user-followed artists from Spotify to Tidal."""
    print("Loading followed artists from Spotify")

    async def get_all_followed_artists() -> List[dict]:
        """Fetch all followed artists from Spotify."""
        followed_artists = []
        after = None

        while True:
            response = await repeat_on_request_error(
                lambda: asyncio.to_thread(spotify_session.current_user_followed_artists, after=after)
            )
            artists = response['artists']['items']
            followed_artists.extend(artists)

            if not response['artists']['cursors'].get('after'):
                break

            after = response['artists']['cursors']['after']

        return followed_artists
    
    async def find_tidal_track_by_spotify_track(spotify_track: dict, tidal_session: tidalapi.Session) -> tidalapi.Track | None:
        """Find a Tidal track that matches a Spotify track."""
        isrc = spotify_track.get("external_ids", {}).get("isrc")
        track_name = spotify_track.get("name", "").strip()
        artist_name = spotify_track.get("artists", [{}])[0].get("name", "").strip()

        # Search by ISRC first
        if isrc:
            formatted_isrc = validate_and_format_isrc(isrc)
            if formatted_isrc:
                try:
                    isrc_results = tidal_session.get_tracks_by_isrc(formatted_isrc)
                    if isrc_results and "tracks" in isrc_results and isrc_results["tracks"]:
                        for tidal_track in isrc_results["tracks"]:
                            if isrc_match(tidal_track, spotify_track):
                                return tidal_track
                except InvalidISRC:
                    # Silently continue with text search for invalid ISRC
                    pass
                except ObjectNotFound:
                    # Silently continue with text search when ISRC not found
                    pass
                except requests.exceptions.HTTPError:
                    # Silently continue with text search for HTTP errors
                    pass

        # Fallback to song name and artist name search
        query = f"{track_name} {artist_name}"
        try:
            search_results = tidal_session.search(query, models=[tidalapi.media.Track])
            if search_results and "tracks" in search_results:
                for tidal_track in search_results["tracks"]:
                    if normalize(tidal_track.name) == normalize(track_name) and artist_match(tidal_track, spotify_track):
                        return tidal_track
        except Exception:
            pass

        # No match found
        return None


    async def match_artist_with_tidal_tracks(spotify_artist: dict, tidal_candidates: List[tidalapi.artist.Artist]):
        """Match a Spotify artist with Tidal artists using their top tracks."""
        # First try exact name match to avoid unnecessary API calls
        for tidal_artist in tidal_candidates:
            if normalize(tidal_artist.name.lower()) == normalize(spotify_artist['name'].lower()):
                return tidal_artist
        
        # If no exact match, try using top tracks
        try:
            top_tracks = spotify_session.artist_top_tracks(spotify_artist['id'])['tracks'][:3]
            if not top_tracks:
                # Fallback to the first candidate if no top tracks
                return tidal_candidates[0] if tidal_candidates else None

            # Only check the first few candidates to reduce API calls
            for tidal_artist in tidal_candidates[:3]:
                for track in top_tracks:
                    tidal_track = await find_tidal_track_by_spotify_track(track, tidal_session)
                    if tidal_track and tidal_artist.id in [a.id for a in tidal_track.artists]:
                        return tidal_artist
                    # Avoid rate limiting
                    await asyncio.sleep(0.1)
        except Exception:
            pass
        
        # Return the first candidate if no track-based match is found
        return tidal_candidates[0] if tidal_candidates else None

    # Fetch all followed artists from Spotify
    spotify_artists = await get_all_followed_artists()
    if not spotify_artists:
        print("No artists followed on Spotify.")
        return

    print(f"Found {len(spotify_artists)} artists followed on Spotify.")

    # Load existing followed artists from Tidal
    tidal_artists = tidal_session.user.favorites.artists()
    tidal_artist_names = set([normalize(artist.name.lower()) for artist in tidal_artists])

    # Filter new artists that are not already followed on Tidal
    new_artists = [artist for artist in spotify_artists if normalize(artist['name'].lower()) not in tidal_artist_names]

    if not new_artists:
        print("All followed artists are already in Tidal.")
        return

    # Add new artists to Tidal
    print(f"Searching and adding {len(new_artists)} new artists to Tidal.")
    failed_artists = []
    for spotify_artist in tqdm(new_artists, desc="Adding new artists to Tidal"):
        try:
            search_results = tidal_session.search(spotify_artist['name'], models=[tidalapi.artist.Artist])
            tidal_candidates = search_results.get('artists', [])
            if not tidal_candidates:
                failed_artists.append(spotify_artist['name'])
                continue
                
            matched_artist = await match_artist_with_tidal_tracks(spotify_artist, tidal_candidates)
            if matched_artist:
                try:
                    tidal_session.user.favorites.add_artist(matched_artist.id)
                    # Add delay between API calls to prevent rate limiting
                    await asyncio.sleep(0.5)
                except requests.exceptions.SSLError:
                    print(f"SSL error adding artist '{spotify_artist['name']}'. Retrying after delay...")
                    await asyncio.sleep(5)
                    try:
                        tidal_session.user.favorites.add_artist(matched_artist.id)
                    except Exception:
                        failed_artists.append(spotify_artist['name'])
                        continue
                except Exception:
                    failed_artists.append(spotify_artist['name'])
                    continue
            else:
                failed_artists.append(spotify_artist['name'])
        except Exception:
            failed_artists.append(spotify_artist['name'])
            continue
    
    if failed_artists:
        print(f"Failed to add {len(failed_artists)} artists to Tidal.")
    else:
        print("Artist synchronization complete.")

async def sync_albums(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    """Synchronize user-saved albums from Spotify to Tidal."""
    print("Loading saved albums from Spotify")

    # Get all saved albums from Spotify
    def _get_saved_albums(offset=0):
        return spotify_session.current_user_saved_albums(offset=offset)

    # Fetch all saved albums from Spotify
    results = await repeat_on_request_error(_fetch_all_from_spotify_in_chunks, _get_saved_albums, item_key="album")
    albums = [item for item in results]

    print(f"Found {len(albums)} albums saved on Spotify")

    # Get existing saved albums from Tidal
    tidal_albums = tidal_session.user.favorites.albums()
    tidal_album_keys = set()
    for tidal_album in tidal_albums:
        if hasattr(tidal_album, 'artists') and tidal_album.artists:
            artist_name = tidal_album.artists[0].name
        else:
            artist_name = ""
        key = (normalize(tidal_album.name.lower()), normalize(artist_name.lower()))
        tidal_album_keys.add(key)

    # Filter new albums to add to Tidal
    def is_album_not_in_tidal(spotify_album):
        spotify_name = normalize(spotify_album['name'].lower())
        spotify_artist = normalize(spotify_album['artists'][0]['name'].lower()) if spotify_album['artists'] else ""
        spotify_key = (spotify_name, spotify_artist)
        return spotify_key not in tidal_album_keys

    new_albums = [album for album in albums if is_album_not_in_tidal(album)]

    if not new_albums:
        print("All saved albums are already in Tidal.")
        return


    # Function to search for an album on Tidal
    async def search_album_on_tidal(spotify_album, tidal_session):
        """Search for an album on Tidal using UPC first, and fallback to other attributes."""
        # Check if Spotify album has a UPC
        upc = spotify_album.get("external_ids", {}).get("upc")
        if upc:
            try:
                # Search for album using UPC
                search_results = tidal_session.get_albums_by_barcode(upc)
                for tidal_album in search_results:
                    if(tidal_album.universal_product_number == upc):
                        return tidal_album
            except ObjectNotFound:
                # UPC not found, continue with text search
                pass

        # Fallback to extended search with query
        artist_name = spotify_album['artists'][0]['name'] if spotify_album['artists'] else ""
        query = f"{spotify_album['name']} {artist_name}"
        search_results = tidal_session.search(query, models=[tidalapi.album.Album])
        if search_results and 'albums' in search_results:
            for tidal_album in search_results['albums']:
                if album_match(tidal_album, spotify_album):
                    return tidal_album  # Best match found
        
        return None

    # Add new albums to Tidal
    successful_adds = 0
    failed_adds = []
    
    for album in tqdm(new_albums, desc="Adding new albums to Tidal"):
        try:
            tidal_album = await search_album_on_tidal(album, tidal_session)
            if tidal_album:
                try:
                    result = tidal_session.user.favorites.add_album(tidal_album.id)
                    if result is not False:
                        successful_adds += 1
                    else:
                        failed_adds.append(album['name'])
                except Exception:
                    failed_adds.append(album['name'])
                    continue
            else:
                failed_adds.append(album['name'])
        except Exception:
            failed_adds.append(album['name'])
            continue
    
    print(f"Album synchronization complete. Successfully added: {successful_adds}, Failed: {len(failed_adds)}")
    if failed_adds:
        print(f"Failed albums: {failed_adds[:10]}{'...' if len(failed_adds) > 10 else ''}")


def sync_playlists_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, playlists, config: dict):
  for spotify_playlist, tidal_playlist in playlists:
    # sync the spotify playlist to tidal
    asyncio.run(sync_playlist(spotify_session, tidal_session, spotify_playlist, tidal_playlist, config) )

def sync_favorites_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    asyncio.run(main=sync_favorites(spotify_session=spotify_session, tidal_session=tidal_session, config=config))

def sync_artists_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    asyncio.run(sync_artists(spotify_session=spotify_session, tidal_session=tidal_session, config=config))

def sync_albums_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    asyncio.run(sync_albums(spotify_session=spotify_session, tidal_session=tidal_session, config=config))

def get_tidal_playlists_wrapper(tidal_session: tidalapi.Session) -> Mapping[str, tidalapi.Playlist]:
    tidal_playlists = asyncio.run(get_all_playlists(tidal_session.user))
    return {playlist.name: playlist for playlist in tidal_playlists}

def pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists: Mapping[str, tidalapi.Playlist]):
    if spotify_playlist['name'] in tidal_playlists:
      # if there's an existing tidal playlist with the name of the current playlist then use that
      tidal_playlist = tidal_playlists[spotify_playlist['name']]
      return (spotify_playlist, tidal_playlist)
    else:
      return (spotify_playlist, None)

def get_user_playlist_mappings(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    results = []
    spotify_playlists = asyncio.run(get_playlists_from_spotify(spotify_session, config))
    tidal_playlists = get_tidal_playlists_wrapper(tidal_session)
    for spotify_playlist in spotify_playlists:
        results.append( pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists) )
    return results

async def get_playlists_from_spotify(spotify_session: spotipy.Spotify, config):
    # get all the playlists from the Spotify account
    playlists = []
    print("Loading Spotify playlists")
    first_results = spotify_session.current_user_playlists()
    exclude_list = set([x.split(':')[-1] for x in config.get('excluded_playlists', [])])
    playlists.extend([p for p in first_results['items']])
    user_id = spotify_session.current_user()['id']

    # get all the remaining playlists in parallel
    if first_results['next']:
        offsets = [ first_results['limit'] * n for n in range(1, math.ceil(first_results['total']/first_results['limit'])) ]
        extra_results = await atqdm.gather( *[asyncio.to_thread(spotify_session.current_user_playlists, offset=offset) for offset in offsets ] )
        for extra_result in extra_results:
            playlists.extend([p for p in extra_result['items']])

    # filter out playlists that don't belong to us or are on the exclude list
    my_playlist_filter = lambda p: p['owner']['id'] == user_id
    exclude_filter = lambda p: not p['id'] in exclude_list
    return list(filter( exclude_filter, filter( my_playlist_filter, playlists )))

def get_playlists_from_config(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    # get the list of playlist sync mappings from the configuration file
    def get_playlist_ids(config):
        return [(item['spotify_id'], item['tidal_id']) for item in config['sync_playlists']]
    output = []
    for spotify_id, tidal_id in get_playlist_ids(config=config):
        try:
            spotify_playlist = spotify_session.playlist(playlist_id=spotify_id)
        except spotipy.SpotifyException as e:
            print(f"Error getting Spotify playlist {spotify_id}")
            raise e
        try:
            tidal_playlist = tidal_session.playlist(playlist_id=tidal_id)
        except Exception as e:
            print(f"Error getting Tidal playlist {tidal_id}")
            raise e
        output.append((spotify_playlist, tidal_playlist))
    return output

