A command line tool for importing your Spotify playlists into Tidal. Due to various performance optimisations, it is particularly suited for periodic synchronisation of very large collections.

Installation
-----------
Clone this git repository and then run:

```bash
python3 -m pip install -e .
```

Setup
-----
0. Rename the file example_config.yml to config.yml
0. Go [here](https://developer.spotify.com/documentation/general/guides/authorization/app-settings/) and register a new app on developer.spotify.com.
0. Copy and paste your client ID and client secret to the Spotify part of the config file
0. Copy and paste the value in 'redirect_uri' of the config file to Redirect URIs at developer.spotify.com and press ADD
0. Enter your Spotify username to the config file

Usage
----
To synchronize all of your Spotify playlists with your Tidal account run the following from the project root directory
Windows ignores python module paths by default, but you can run them using `python3 -m spotify_to_tidal`

```bash
spotify_to_tidal
```

You can also just synchronize a specific playlist by doing the following:

```bash
spotify_to_tidal --uri 1ABCDEqsABCD6EaABCDa0a # accepts playlist id or full playlist uri
```

or sync just your 'Liked Songs' with:

```bash
spotify_to_tidal --sync-favorites
```

or sync just your saved albums with:

```bash
spotify_to_tidal --sync-albums
```

or sync just your saved artists with:

```bash
spotify_to_tidal --sync-artists
```

See example_config.yml for more configuration options, and `spotify_to_tidal --help` for more options.

Reading playlists without a Spotify API subscription (spotify-scraper mode)
--------------------------------------------------------------------------
This branch reads the source Spotify playlist via the [`spotifyscraper`](https://pypi.org/project/spotifyscraper/)
library instead of the official Spotify Web API. This avoids the
`403 – Active premium subscription required for the owner of the app` error that
the Web API returns when the account behind the Spotify app is no longer
premium. No Spotify client id/secret is needed for this path.

The Tidal side (matching, searching, creating/updating the playlist) is
unchanged, so the usual command still works and updates an existing Tidal
playlist of the same name when tracks change:

```bash
spotify_to_tidal --uri 2xAZhFiPSUhb5Mi4ir9Lht   # id, spotify:playlist:<id>, or open.spotify.com url
```

Limitations of scraper mode: only reading *public* playlists is supported
(`--uri`, or `sync_playlists` mappings in the config). ISRC and album-artist
metadata are not exposed by the scraper, so track matching relies on the
duration + name + artist heuristic rather than exact ISRC matches. Syncing
favorites / followed artists / saved albums / your own account's playlist list
requires the authenticated Web API and is not available in this mode.

---

#### Join our amazing community as a code contributor
<br><br>
<a href="https://github.com/spotify2tidal/spotify_to_tidal/graphs/contributors">
  <img class="dark-light" src="https://contrib.rocks/image?repo=spotify2tidal/spotify_to_tidal&anon=0&columns=25&max=100&r=true" />
</a>
