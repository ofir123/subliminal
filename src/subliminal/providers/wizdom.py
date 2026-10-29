"""Hebrew subtitles from Wizdom."""

from __future__ import annotations

import io
import logging
import os
from typing import TYPE_CHECKING, Any, ClassVar
from zipfile import ZipFile

from babelfish import Language  # type: ignore[import-untyped]
from requests import Session

from subliminal.cache import SHOW_EXPIRATION_TIME, region
from subliminal.exceptions import NotInitializedProviderError, ProviderError
from subliminal.matches import guess_matches
from subliminal.subtitle import Subtitle
from subliminal.utils import safely_guessit, sanitize
from subliminal.video import Episode, Movie

from . import Provider

if TYPE_CHECKING:
    from collections.abc import Set

    from subliminal.video import Video

logger = logging.getLogger(__name__)


class WizdomSubtitle(Subtitle):
    """Wizdom subtitle and release information."""

    provider_name: ClassVar[str] = 'wizdom'

    def __init__(
        self,
        language: Language,
        subtitle_id: str,
        *,
        hearing_impaired: bool = False,
        page_link: str | None = None,
        series: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        title: str | None = None,
        imdb_id: str | None = None,
        release: str = '',
    ) -> None:
        super().__init__(language, str(subtitle_id), hearing_impaired=hearing_impaired, page_link=page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.imdb_id = imdb_id
        self.release = release

    @property
    def info(self) -> str:
        """Information about the subtitle."""
        return self.release or self.id

    def get_matches(self, video: Video) -> set[str]:
        """Get the matches against the video."""
        matches: set[str] = set()
        if isinstance(video, Episode):
            if sanitize(self.series or self.title) in {sanitize(n) for n in [video.series, *video.alternative_series]}:
                matches.add('series')
            if self.season == video.season:
                matches.add('season')
            if video.episodes == [self.episode]:
                matches.add('episode')
            if self.imdb_id and self.imdb_id == video.external_ids.get('series_imdb_id'):
                matches.add('series_imdb_id')
            matches |= guess_matches(video, safely_guessit(self.release, {'type': 'episode'}), partial=True)
        elif isinstance(video, Movie):
            matches |= guess_matches(video, safely_guessit(self.release, {'type': 'movie'}), partial=True)
            if sanitize(self.title) in {sanitize(n) for n in [video.title, *video.alternative_titles]}:
                matches.add('title')
        return matches


class WizdomProvider(Provider[WizdomSubtitle]):
    """Wizdom Provider."""

    languages: ClassVar[Set[Language]] = {Language('heb')}
    subtitle_class: ClassVar = WizdomSubtitle
    server_url: ClassVar[str] = 'https://wizdom.xyz'
    _tmdb_api_key: ClassVar[str] = 'a51ee051bcd762543373903de296e0a3'

    def __init__(self, *, timeout: int = 10) -> None:
        self.timeout = timeout
        self.session: Session | None = None

    def initialize(self) -> None:
        """Initialize the provider."""
        self.session = Session()
        self.session.headers['User-Agent'] = self.user_agent

    def terminate(self) -> None:
        """Terminate the provider."""
        if self.session is None:
            raise NotInitializedProviderError
        self.session.close()
        self.session = None

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def _search_imdb_id(self, title: str, year: int | None, is_movie: bool) -> str | None:  # noqa: FBT001
        """Resolve an IMDb ID through TMDB when the video has none."""
        if self.session is None:
            raise NotInitializedProviderError
        category = 'movie' if is_movie else 'tv'
        params: dict[str, Any] = {'api_key': self._tmdb_api_key, 'query': title, 'language': 'en'}
        if year:
            params['year' if is_movie else 'first_air_date_year'] = year
        r = self.session.get(f'https://api.tmdb.org/3/search/{category}', params=params, timeout=self.timeout)
        r.raise_for_status()
        results = r.json().get('results', [])
        if not results or not results[0].get('id'):
            return None
        tmdb_id = results[0]['id']
        suffix = '' if is_movie else '/external_ids'
        r = self.session.get(
            f'https://api.tmdb.org/3/{category}/{tmdb_id}{suffix}',
            params={'api_key': self._tmdb_api_key, 'language': 'en'},
            timeout=self.timeout,
        )
        r.raise_for_status()
        imdb_id = r.json().get('imdb_id')
        return str(imdb_id) if imdb_id else None

    def query(
        self,
        title: str,
        *,
        season: int | None = None,
        episode: int | None = None,
        year: int | None = None,
        imdb_id: str | None = None,
    ) -> list[WizdomSubtitle]:
        """Query subtitles by title or IMDb ID.

        :param str title: movie or series title.
        :param (int | None) season: season number, including season zero.
        :param (int | None) episode: episode number.
        :param (int | None) year: movie or series year.
        :param (str | None) imdb_id: known movie or series IMDb ID.
        :return: matching Hebrew subtitles.
        :rtype: list[WizdomSubtitle]
        """
        if self.session is None:
            raise NotInitializedProviderError
        is_movie = season is None or episode is None
        imdb_id = imdb_id or self._search_imdb_id(title, year, is_movie)
        if not imdb_id:
            return []
        r = self.session.get(f'{self.server_url}/api/releases/{imdb_id}', timeout=self.timeout)
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        results = data.get('subs', [])
        if not is_movie:
            if isinstance(results, list):
                results = results[season] if season is not None and 0 <= season < len(results) else {}
            elif isinstance(results, dict):
                results = results.get(str(season), {})
            else:
                return []
            results = results.get(str(episode), []) if isinstance(results, dict) else []
        if not isinstance(results, list):
            return []

        category = 'movies' if is_movie else 'series'
        page_link = f'{self.server_url}/#/{category}/{imdb_id}'
        subtitles: dict[str, WizdomSubtitle] = {}
        for result in results:
            subtitle_id = str(result['id'])
            subtitles[subtitle_id] = self.subtitle_class(
                Language('heb'),
                subtitle_id,
                page_link=page_link,
                series=None if is_movie else title,
                season=season,
                episode=episode,
                title=title,
                imdb_id=imdb_id,
                release=result['version'],
            )
        return list(subtitles.values())

    def list_subtitles(self, video: Video, languages: Set[Language]) -> list[WizdomSubtitle]:
        """List all the subtitles for the video."""
        if not self.check_languages(languages):
            return []
        season = episode = None
        if isinstance(video, Episode):
            titles = [video.series, *video.alternative_series]
            season, episode = video.season, video.episode
            imdb_id = video.external_ids.get('series_imdb_id')
        elif isinstance(video, Movie):
            titles = [video.title, *video.alternative_titles]
            imdb_id = video.external_ids.get('imdb_id')
        else:
            return []
        for title in titles:
            subtitles = self.query(title, season=season, episode=episode, year=video.year, imdb_id=imdb_id)
            if subtitles:
                return subtitles
        return []

    def download_subtitle(self, subtitle: WizdomSubtitle) -> None:
        """Download the content of the subtitle."""
        if self.session is None:
            raise NotInitializedProviderError
        r = self.session.get(
            f'{self.server_url}/api/files/sub/{subtitle.subtitle_id}',
            headers={'Referer': subtitle.page_link},
            timeout=self.timeout,
        )
        r.raise_for_status()
        with ZipFile(io.BytesIO(r.content)) as zf:
            names = [n for n in zf.namelist() if os.path.splitext(n)[1].lower() in {'.srt', '.sub'}]
            if len(names) != 1:
                msg = 'Expected exactly one subtitle file in the archive'
                raise ProviderError(msg)
            subtitle.set_content(zf.read(names[0]))
