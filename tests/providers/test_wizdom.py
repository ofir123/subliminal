from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from babelfish import Language  # type: ignore[import-untyped]
from requests import HTTPError

from subliminal import download_best_subtitles, save_subtitles
from subliminal.exceptions import NotInitializedProviderError, ProviderError
from subliminal.extensions import provider_manager
from subliminal.providers.wizdom import WizdomProvider, WizdomSubtitle
from subliminal.video import Episode, Movie, Video

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

HEBREW = Language('heb')
RELEASE = 'Game.of.Thrones.S03E10.720p.HDTV.x264-EVOLVE'
SRT = '1\r\n00:00:01,000 --> 00:00:02,000\r\nשלום\r\n\r\n'.encode()  # noqa: RUF001
RESULT = {'id': 3748, 'version': RELEASE}


def archive(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> WizdomProvider:
    session = Mock(headers={})
    monkeypatch.setattr('subliminal.providers.wizdom.Session', Mock(return_value=session))
    instance = WizdomProvider(timeout=7)
    instance.initialize()
    return instance


def mock_session(provider: WizdomProvider) -> Mock:
    return cast('Mock', provider.session)


def test_get_matches_movie(movies: dict[str, Movie]) -> None:
    subtitle = WizdomSubtitle(HEBREW, '18947', title="Ender's Game", release='Enders.Game.2013.720p.BluRay.x264-SPARKS')
    assert subtitle.get_matches(movies['enders_game']) == {
        'country',
        'title',
        'year',
        'resolution',
        'video_codec',
        'source',
        'release_group',
    }
    assert subtitle.id == subtitle.subtitle_id == '18947'
    assert subtitle.info == subtitle.release
    assert WizdomSubtitle(HEBREW, '1').info == '1'


def test_get_matches_episode(episodes: dict[str, Episode]) -> None:
    subtitle = WizdomSubtitle(
        HEBREW,
        '3748',
        series='Game of Thrones',
        season=3,
        episode=10,
        title='Mhysa',
        imdb_id='tt0944947',
        release=RELEASE,
    )
    assert subtitle.get_matches(episodes['got_s03e10']) == {
        'series',
        'episode',
        'season',
        'video_codec',
        'resolution',
        'series_imdb_id',
    }


def test_get_matches_no_match(movies: dict[str, Movie]) -> None:
    subtitle = WizdomSubtitle(HEBREW, '3748', title='Game of Thrones', release=RELEASE)
    assert subtitle.get_matches(movies['man_of_steel']) == {'country', 'video_codec', 'resolution'}


def test_matches_alternative_title_and_specials() -> None:
    video = Episode('special.mkv', 'Show', 0, 1, alternative_series=['Alias'])
    subtitle = WizdomSubtitle(HEBREW, '1', series='Alias', season=0, episode=1)
    assert {'series', 'season', 'episode'} <= subtitle.get_matches(video)
    movie = Movie('movie.mkv', 'Movie', alternative_titles=['Alias'])
    assert 'title' in WizdomSubtitle(HEBREW, '1', title='Alias').get_matches(movie)
    video.episodes = [1, 2]
    assert 'episode' not in subtitle.get_matches(video)
    assert subtitle.get_matches(Video('unknown.mkv')) == set()


@pytest.mark.parametrize('is_movie', [True, False])
def test_search_imdb_id(provider: WizdomProvider, is_movie: bool) -> None:
    mock_session(provider).get.side_effect = [
        Mock(json=Mock(return_value={'results': [{'id': 42}]})),
        Mock(json=Mock(return_value={'imdb_id': 'tt123'})),
    ]
    assert provider._search_imdb_id("A & B's", 2012, is_movie) == 'tt123'
    calls = mock_session(provider).get.call_args_list
    category = 'movie' if is_movie else 'tv'
    assert calls[0].args == (f'https://api.tmdb.org/3/search/{category}',)
    assert calls[0].kwargs['params']['query'] == "A & B's"
    assert calls[0].kwargs['params']['year' if is_movie else 'first_air_date_year'] == 2012
    assert calls[0].kwargs['timeout'] == 7
    suffix = '' if is_movie else '/external_ids'
    assert calls[1].args == (f'https://api.tmdb.org/3/{category}/42{suffix}',)
    assert calls[1].kwargs['timeout'] == 7


@pytest.mark.parametrize('results', [[], [{}]])
def test_search_imdb_id_no_result(provider: WizdomProvider, results: list) -> None:
    mock_session(provider).get.return_value.json.return_value = {'results': results}
    assert provider._search_imdb_id('Missing', None, True) is None


@pytest.mark.parametrize('imdb_id', [None, ''])
def test_search_imdb_id_null(provider: WizdomProvider, imdb_id: str | None) -> None:
    mock_session(provider).get.side_effect = [
        Mock(json=Mock(return_value={'results': [{'id': 42}]})),
        Mock(json=Mock(return_value={'imdb_id': imdb_id})),
    ]
    assert provider._search_imdb_id('Missing', None, True) is None


@pytest.mark.parametrize(
    'subs',
    [
        {'3': {'10': [RESULT, RESULT]}},
        [{}, {}, {}, {'10': [RESULT, RESULT]}],
    ],
)
def test_query_episode_formats(provider: WizdomProvider, subs: dict | list) -> None:
    mock_session(provider).get.return_value.json.return_value = {'subs': subs}
    subtitles = provider.query('Game of Thrones', season=3, episode=10, imdb_id='tt0944947')
    assert len(subtitles) == 1
    assert subtitles[0].id == '3748'
    assert subtitles[0].language == HEBREW
    assert subtitles[0].series == 'Game of Thrones'
    assert subtitles[0].page_link == 'https://wizdom.xyz/#/series/tt0944947'
    mock_session(provider).get.assert_called_once_with('https://wizdom.xyz/api/releases/tt0944947', timeout=7)


@pytest.mark.parametrize(
    ('subs', 'season'), [([], 0), ([{}], 1), ([{}], -1), ({}, 3), ([None], 0), (None, 3), ({'3': {'10': None}}, 3)]
)
def test_query_missing_season_or_episode(provider: WizdomProvider, subs: object, season: int) -> None:
    mock_session(provider).get.return_value.json.return_value = {'subs': subs}
    assert provider.query('Show', season=season, episode=10, imdb_id='tt1') == []


def test_query_specials(provider: WizdomProvider) -> None:
    mock_session(provider).get.return_value.json.return_value = {'subs': [{'1': [RESULT]}]}
    subtitle = provider.query('Show', season=0, episode=1, imdb_id='tt1')[0]
    assert subtitle.season == 0
    assert subtitle.page_link is not None
    assert '/series/' in subtitle.page_link


def test_query_movie(provider: WizdomProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    search = Mock(return_value='tt1')
    monkeypatch.setattr(provider, '_search_imdb_id', search)
    mock_session(provider).get.return_value.json.return_value = {'subs': [RESULT]}
    subtitle = provider.query('Movie', year=2013)[0]
    search.assert_called_once_with('Movie', 2013, True)
    assert subtitle.title == 'Movie'
    assert subtitle.page_link == 'https://wizdom.xyz/#/movies/tt1'


def test_query_no_imdb_id(provider: WizdomProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider, '_search_imdb_id', Mock(return_value=None))
    assert provider.query('Unknown') == []
    mock_session(provider).get.assert_not_called()


@pytest.mark.parametrize('data', [[], None, {}, {'subs': None}])
def test_query_empty_response(provider: WizdomProvider, data: object) -> None:
    mock_session(provider).get.return_value.json.return_value = data
    assert provider.query('Unknown', imdb_id='tt1') == []


def test_query_invalid_json_and_http_error(provider: WizdomProvider) -> None:
    response = mock_session(provider).get.return_value
    response.json.side_effect = ValueError
    assert provider.query('Unknown', imdb_id='tt1') == []
    response.raise_for_status.side_effect = HTTPError
    with pytest.raises(HTTPError):
        provider.query('Unknown', imdb_id='tt1')


@pytest.mark.parametrize(
    ('video', 'key', 'title'),
    [
        (Movie('movie.mkv', 'Movie', external_ids={'imdb_id': 'tt1'}), 'imdb_id', 'Movie'),
        (Episode('show.mkv', 'Show', 1, 2, external_ids={'series_imdb_id': 'tt1'}), 'series_imdb_id', 'Show'),
    ],
)
def test_list_subtitles_external_ids(
    provider: WizdomProvider,
    monkeypatch: pytest.MonkeyPatch,
    video: Video,
    key: Literal['imdb_id', 'series_imdb_id'],
    title: str,
) -> None:
    subtitle = WizdomSubtitle(HEBREW, '1')
    query = Mock(return_value=[subtitle])
    monkeypatch.setattr(provider, 'query', query)
    assert provider.list_subtitles(video, {HEBREW}) == [subtitle]
    assert query.call_args.args == (title,)
    assert query.call_args.kwargs['imdb_id'] == video.external_ids[key]


@pytest.mark.parametrize(
    'video',
    [
        Movie('movie.mkv', 'Movie', alternative_titles=['Alias']),
        Episode('show.mkv', 'Show', 1, 2, alternative_series=['Alias']),
    ],
)
def test_list_subtitles_alternative_title(
    provider: WizdomProvider, monkeypatch: pytest.MonkeyPatch, video: Video
) -> None:
    subtitle = WizdomSubtitle(HEBREW, '1')
    query = Mock(side_effect=[[], [subtitle]])
    monkeypatch.setattr(provider, 'query', query)
    assert provider.list_subtitles(video, {HEBREW}) == [subtitle]
    assert query.call_args.args == ('Alias',)


def test_list_subtitles_no_results(provider: WizdomProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    query = Mock(return_value=[])
    monkeypatch.setattr(provider, 'query', query)
    assert provider.list_subtitles(Movie('movie.mkv', 'Movie'), {HEBREW}) == []
    assert provider.list_subtitles(Video('unknown.mkv'), {HEBREW}) == []
    assert provider.list_subtitles(Movie('movie.mkv', 'Movie'), {Language('eng')}) == []
    assert query.call_count == 1


@pytest.mark.parametrize('names', [[], ['README.txt'], ['a.srt', 'b.sub']])
def test_download_invalid_archive(provider: WizdomProvider, names: list[str]) -> None:
    mock_session(provider).get.return_value.content = archive(dict.fromkeys(names, SRT))
    with pytest.raises(ProviderError, match='exactly one'):
        provider.download_subtitle(WizdomSubtitle(HEBREW, '1'))


def test_download_subtitle(provider: WizdomProvider) -> None:
    mock_session(provider).get.return_value.content = archive({'README.txt': b'info', 'episode.SRT': SRT})
    subtitle = WizdomSubtitle(HEBREW, '1', page_link='https://wizdom.xyz/#/series/tt1')
    provider.download_subtitle(subtitle)
    assert subtitle.content == SRT.replace(b'\r\n', b'\n')
    assert subtitle.is_valid()
    assert 'שלום' in subtitle.text
    mock_session(provider).get.assert_called_once_with(
        'https://wizdom.xyz/api/files/sub/1', headers={'Referer': subtitle.page_link}, timeout=7
    )


def test_lifecycle(provider: WizdomProvider) -> None:
    session = mock_session(provider)
    assert session.headers['User-Agent'] == provider.user_agent
    provider.terminate()
    session.close.assert_called_once()
    assert provider.session is None


def test_uninitialized() -> None:
    provider = WizdomProvider()
    for operation in [
        provider.terminate,
        lambda: provider.query('Show'),
        lambda: provider._search_imdb_id('Show', None, True),
        lambda: provider.download_subtitle(WizdomSubtitle(HEBREW, '1')),
    ]:
        with pytest.raises(NotInitializedProviderError):
            operation()


def test_provider_pool_download_and_save(
    provider: WizdomProvider, tmp_path: Path, episodes: dict[str, Episode]
) -> None:
    assert provider_manager['wizdom'].plugin is WizdomProvider
    mock_session(provider).get.side_effect = [
        Mock(json=Mock(return_value={'subs': {'3': {'10': [RESULT]}}})),
        Mock(content=archive({'episode.srt': SRT})),
    ]
    video = episodes['got_s03e10']
    subtitles = download_best_subtitles({video}, {HEBREW}, providers=['wizdom'])[video]
    assert len(subtitles) == 1
    assert subtitles[0].is_valid()
    assert save_subtitles(video, subtitles, directory=tmp_path) == subtitles
    saved = list(tmp_path.glob('*.he.srt'))
    assert len(saved) == 1
    assert 'שלום' in saved[0].read_text()
