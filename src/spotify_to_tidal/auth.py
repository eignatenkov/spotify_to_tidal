#!/usr/bin/env python3

import sys
import spotipy
import tidalapi
import webbrowser
import yaml

__all__ = [
    'open_spotify_session',
    'open_tidal_session'
]

SPOTIFY_SCOPES = 'playlist-read-private, user-library-read, user-follow-read'

# Tidal OAuth device-flow client.
#
# The client baked into tidalapi 0.8.2 (id "zU4XHVVkc2tDPo4t", internal id 3235) has been
# deprecated by Tidal: an existing access token still validates, but the token-refresh endpoint
# now rejects it with `invalid_client / "Client id 3235 not found"`. That made every stored
# session unrefreshable, so each run fell back to an interactive browser login -- which the
# weekly cron cannot satisfy and hangs on until the timeout.
#
# These credentials are tidalapi 0.8.11's default device-flow client, which Tidal still accepts
# for device authorization, token exchange, AND refresh. We override them here instead of
# upgrading tidalapi so the rest of the sync logic keeps running against the pinned 0.8.2 API.
TIDAL_CLIENT_ID = 'fX2JxdmntZWK0ixT'
TIDAL_CLIENT_SECRET = '1Nn9AfDAjxrgJFJbKNWLeAyKGVGmINuXPPLHVXAvxAg='

def _tidal_config() -> tidalapi.Config:
    config = tidalapi.Config()
    config.client_id = TIDAL_CLIENT_ID
    config.client_secret = TIDAL_CLIENT_SECRET
    return config

def _save_tidal_session(session: tidalapi.Session) -> None:
    with open('.session.yml', 'w') as f:
        yaml.dump({'session_id': session.session_id,
                   'token_type': session.token_type,
                   'access_token': session.access_token,
                   'refresh_token': session.refresh_token}, f)

def open_spotify_session(config) -> spotipy.Spotify:
    credentials_manager = spotipy.SpotifyOAuth(username=config['username'],
       scope=SPOTIFY_SCOPES,
       client_id=config['client_id'],
       client_secret=config['client_secret'],
       redirect_uri=config['redirect_uri'],
       requests_timeout=2,
       open_browser=config.get('open_browser', True))
    try:
        credentials_manager.get_access_token(as_dict=False)
    except spotipy.SpotifyOauthError:
        sys.exit("Error opening Spotify sesion; could not get token for username: ".format(config['username']))

    return spotipy.Spotify(oauth_manager=credentials_manager)

def open_tidal_session(config = None) -> tidalapi.Session:
    try:
        with open('.session.yml', 'r') as session_file:
            previous_session = yaml.safe_load(session_file)
    except OSError:
        previous_session = None

    session = tidalapi.Session(config=config if config else _tidal_config())
    if previous_session:
        try:
            if session.load_oauth_session(token_type= previous_session['token_type'],
                                   access_token=previous_session['access_token'],
                                   refresh_token=previous_session['refresh_token'] ):
                # load_oauth_session transparently refreshes an expired access token via the
                # refresh token; persist the refreshed token so the file stays current.
                _save_tidal_session(session)
                return session
        except Exception as e:
            print("Error loading previous Tidal Session: \n" + str(e) )

    login, future = session.login_oauth()
    print('Login with the webbrowser: ' + login.verification_uri_complete)
    url = login.verification_uri_complete
    if not url.startswith('https://'):
        url = 'https://' + url
    webbrowser.open(url)
    future.result()
    _save_tidal_session(session)
    return session


