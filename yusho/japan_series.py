from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import comb
from typing import Iterable

import pandas as pd

from .simulation import odds_ratio


@dataclass(frozen=True)
class JapanSeriesResult:
    team_a: str
    team_b: str
    team_a_probability: float
    team_b_probability: float
    champion_dates: pd.DataFrame
    team_date_probabilities: pd.DataFrame
    series_schedule: pd.DataFrame


def _japan_series_dates(
    start_date: date,
    max_game: int,
    postponed_games: Iterable[int],
) -> tuple[dict[int, pd.Timestamp], dict[int, pd.Timestamp]]:
    """Build dates while applying the NPB rain-delay travel-day rules."""
    base_offsets = (0, 1, 3, 4, 5, 7, 8, 9, 11)
    counts = [0] * (max_game + 1)
    for game_number in postponed_games:
        counts[game_number] += 1

    original_dates = {
        game_number: pd.Timestamp(
            start_date + timedelta(days=base_offsets[game_number - 1])
        )
        for game_number in range(1, max_game + 1)
    }
    actual_dates: dict[int, pd.Timestamp] = {}
    early_postponement = any(game_number <= 5 for game_number in postponed_games)
    for game_number in range(1, max_game + 1):
        if game_number == 1:
            gap = 0
            previous_date = pd.Timestamp(start_date)
        else:
            previous_date = actual_dates[game_number - 1]
            if game_number == 3:
                gap = 2
            elif game_number == 6:
                gap = 1 if early_postponement else 2
            elif game_number == 9:
                gap = 2
            else:
                gap = 1
        actual_dates[game_number] = previous_date + timedelta(
            days=gap + counts[game_number]
        )
    return actual_dates, original_dates


def japan_series_schedule(
    start_date: date,
    extra_games: int = 0,
    postponed_games: Iterable[int] = (),
) -> pd.DataFrame:
    """Return the scheduled games plus official tie-breaker games."""
    if not 0 <= extra_games <= 2:
        raise ValueError("追加試合数は0試合から2試合まで指定してください")
    postponed_games = tuple(int(game) for game in postponed_games)
    max_game = 7 + extra_games
    if any(game < 1 or game > max_game for game in postponed_games):
        raise ValueError("雨天順延の試合番号が日程の範囲外です")
    offsets = (0, 1, 3, 4, 5, 7, 8)
    home_teams = ("DB", "DB", "H", "H", "H", "DB", "DB")
    venues = (
        "横浜スタジアム",
        "横浜スタジアム",
        "みずほPayPayドーム福岡",
        "みずほPayPayドーム福岡",
        "みずほPayPayドーム福岡",
        "横浜スタジアム",
        "横浜スタジアム",
    )
    start_times = (
        "18:30",
        "18:00",
        "18:00",
        "18:00",
        "18:00",
        "18:30",
        "18:00",
    )
    actual_dates, original_dates = _japan_series_dates(
        start_date,
        max_game,
        postponed_games,
    )
    rows = [
        {
            "Game": index,
            "Date": actual_dates[index],
            "OriginalDate": original_dates[index],
            "PostponedDays": (actual_dates[index] - original_dates[index]).days,
            "HomeTeam": home_team,
            "Venue": venue,
            "StartTime": start_time,
        }
        for index, (_offset, home_team, venue, start_time) in enumerate(
            zip(offsets, home_teams, venues, start_times),
            start=1,
        )
    ]
    if extra_games >= 1:
        rows.append(
            {
                "Game": 8,
                "Date": actual_dates[8],
                "OriginalDate": original_dates[8],
                "PostponedDays": (actual_dates[8] - original_dates[8]).days,
                "HomeTeam": "DB",
                "Venue": "横浜スタジアム",
                "StartTime": "未定",
            }
        )
    if extra_games >= 2:
        rows.append(
            {
                "Game": 9,
                "Date": actual_dates[9],
                "OriginalDate": original_dates[9],
                "PostponedDays": (actual_dates[9] - original_dates[9]).days,
                "HomeTeam": "H",
                "Venue": "みずほPayPayドーム福岡",
                "StartTime": "未定",
            }
        )
    return pd.DataFrame(rows)


def calculate_japan_series(
    team_a: str,
    team_b: str,
    start_date: date,
    team_a_win_rate: float = 0.5,
    team_b_win_rate: float = 0.5,
    team_a_wins: int = 0,
    team_b_wins: int = 0,
    games_played: int | None = None,
    ties: int = 0,
    postponed_games: Iterable[int] = (),
) -> JapanSeriesResult:
    """Calculate best-of-seven probabilities with the binomial distribution."""
    if team_a == team_b:
        raise ValueError("代表チームは異なるチームを指定してください")
    if not 0 <= team_a_wins <= 3 or not 0 <= team_b_wins <= 3:
        raise ValueError("現在の勝利数は0勝から3勝まで指定してください")
    if not 0 <= ties <= 2:
        raise ValueError("現在の引き分け数は0試合から2試合まで指定してください")
    if games_played is None:
        games_played = team_a_wins + team_b_wins + ties
    if games_played != team_a_wins + team_b_wins + ties or not 0 <= games_played <= 8:
        raise ValueError("消化試合数は勝利数と引き分け数の合計で、8試合以下で指定してください")

    team_a_win_rate = _clamp_rate(team_a_win_rate)
    team_b_win_rate = _clamp_rate(team_b_win_rate)
    game_win_probability = odds_ratio(team_a_win_rate, team_b_win_rate)
    game_loss_probability = 1 - game_win_probability
    schedule = japan_series_schedule(
        start_date,
        extra_games=min(2, ties),
        postponed_games=postponed_games,
    )

    champion_rows: list[dict[str, object]] = []
    team_date_rows: list[dict[str, object]] = []
    team_a_probability = 0.0
    team_b_probability = 0.0
    needed_a = 4 - team_a_wins
    needed_b = 4 - team_b_wins
    for series_length, row in enumerate(schedule.itertuples(index=False), start=1):
        if series_length <= games_played:
            continue
        games_before_decision = series_length - games_played - 1
        wins_needed_before_decision_a = needed_a - 1
        wins_needed_before_decision_b = needed_b - 1
        a_probability = (
            _binomial_coefficient(
                games_before_decision,
                wins_needed_before_decision_a,
            )
            * game_win_probability**needed_a
            * game_loss_probability
            ** (games_before_decision - wins_needed_before_decision_a)
        )
        b_probability = (
            _binomial_coefficient(
                games_before_decision,
                wins_needed_before_decision_b,
            )
            * game_loss_probability**needed_b
            * game_win_probability
            ** (games_before_decision - wins_needed_before_decision_b)
        )
        if a_probability + b_probability <= 0:
            continue
        champion_date = pd.Timestamp(row.Date)
        date_label = f"{champion_date.month}/{champion_date.day}"
        champion_rows.append(
            {
                "Date": champion_date,
                "DateLabel": date_label,
                "Probability": a_probability + b_probability,
            }
        )
        team_date_rows.extend(
            [
                {
                    "Date": champion_date,
                    "Winner": team_a,
                    "Probability": a_probability,
                },
                {
                    "Date": champion_date,
                    "Winner": team_b,
                    "Probability": b_probability,
                },
            ]
        )
        team_a_probability += a_probability
        team_b_probability += b_probability

    champion_dates = pd.DataFrame(champion_rows)
    team_date_probabilities = pd.DataFrame(team_date_rows)

    return JapanSeriesResult(
        team_a=team_a,
        team_b=team_b,
        team_a_probability=team_a_probability,
        team_b_probability=team_b_probability,
        champion_dates=champion_dates,
        team_date_probabilities=team_date_probabilities,
        series_schedule=schedule,
    )


def completed_japan_series_score(
    schedule: pd.DataFrame,
    team_a: str,
    team_b: str,
) -> tuple[int, int, int, int]:
    """Extract official final wins, completed games, and ties."""
    if schedule.empty:
        return 0, 0, 0, 0

    team_a_wins = 0
    team_b_wins = 0
    games_played = 0
    ties = 0
    for row in schedule.itertuples(index=False):
        if str(getattr(row, "Status", "")) != "final":
            continue
        home_team = str(getattr(row, "HomeTeam", ""))
        away_team = str(getattr(row, "AwayTeam", ""))
        if {home_team, away_team} != {team_a, team_b}:
            continue
        score_home = pd.to_numeric(getattr(row, "Score1", pd.NA), errors="coerce")
        score_away = pd.to_numeric(getattr(row, "Score2", pd.NA), errors="coerce")
        if pd.isna(score_home) or pd.isna(score_away):
            continue
        games_played += 1
        if score_home == score_away:
            ties += 1
            continue
        home_won = score_home > score_away
        winner = home_team if home_won else away_team
        if winner == team_a:
            team_a_wins += 1
        elif winner == team_b:
            team_b_wins += 1
    return team_a_wins, team_b_wins, games_played, ties


def japan_series_canceled_games(
    schedule: pd.DataFrame,
    team_a: str,
    team_b: str,
    official_start_date: date,
) -> tuple[int, ...]:
    """Infer Japan Series game numbers from official canceled rows.

    NPB publishes a canceled game as a row with the original date and later
    publishes the postponed fixture as another row. The home-team pattern and
    the official base dates let us associate each canceled row with a series
    game, including repeated cancellations of the same game.
    """
    required_columns = {"Date", "HomeTeam", "AwayTeam", "Status"}
    if schedule.empty or not required_columns.issubset(schedule.columns):
        return ()

    base_home_teams = ("DB", "DB", "H", "H", "H", "DB", "DB", "DB", "H")
    canceled_games: list[int] = []
    frame = schedule.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"]).sort_values("Date")

    for row in frame.itertuples(index=False):
        if str(getattr(row, "Status", "")) != "canceled":
            continue
        if {
            str(getattr(row, "HomeTeam", "")),
            str(getattr(row, "AwayTeam", "")),
        } != {team_a, team_b}:
            continue

        row_date = pd.Timestamp(getattr(row, "Date")).date()
        row_home = str(getattr(row, "HomeTeam", ""))
        current_dates, _ = _japan_series_dates(
            official_start_date,
            len(base_home_teams),
            canceled_games,
        )
        candidates: list[tuple[int, date]] = []
        for game_number, home_team in enumerate(
            base_home_teams,
            start=1,
        ):
            if home_team != row_home:
                continue
            expected_date = current_dates[game_number].date()
            candidates.append((game_number, expected_date))

        if not candidates:
            continue
        exact = [game for game in candidates if game[1] == row_date]
        game_number = (
            exact[0][0]
            if exact
            else min(candidates, key=lambda item: abs((item[1] - row_date).days))[0]
        )
        canceled_games.append(game_number)

    return tuple(canceled_games)


def _clamp_rate(value: float) -> float:
    return min(0.999, max(0.001, float(value)))


def _binomial_coefficient(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return comb(n, k)
