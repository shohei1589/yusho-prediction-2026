from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import comb

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


def japan_series_schedule(start_date: date) -> pd.DataFrame:
    """Return the seven possible Japan Series game dates and home sides."""
    offsets = (0, 1, 3, 4, 5, 7, 8)
    home_sides = (
        "セ・リーグ代表",
        "セ・リーグ代表",
        "パ・リーグ代表",
        "パ・リーグ代表",
        "パ・リーグ代表",
        "セ・リーグ代表",
        "セ・リーグ代表",
    )
    return pd.DataFrame(
        [
            {
                "Game": index,
                "Date": pd.Timestamp(start_date + timedelta(days=offset)),
                "HomeSide": home_side,
            }
            for index, (offset, home_side) in enumerate(
                zip(offsets, home_sides),
                start=1,
            )
        ]
    )


def calculate_japan_series(
    team_a: str,
    team_b: str,
    start_date: date,
    team_a_win_rate: float = 0.5,
    team_b_win_rate: float = 0.5,
    team_a_wins: int = 0,
    team_b_wins: int = 0,
) -> JapanSeriesResult:
    """Calculate best-of-seven probabilities with the binomial distribution."""
    if team_a == team_b:
        raise ValueError("代表チームは異なるチームを指定してください")
    if not 0 <= team_a_wins <= 3 or not 0 <= team_b_wins <= 3:
        raise ValueError("現在の勝利数は0勝から3勝まで指定してください")

    team_a_win_rate = _clamp_rate(team_a_win_rate)
    team_b_win_rate = _clamp_rate(team_b_win_rate)
    game_win_probability = odds_ratio(team_a_win_rate, team_b_win_rate)
    game_loss_probability = 1 - game_win_probability
    schedule = japan_series_schedule(start_date)

    champion_rows: list[dict[str, object]] = []
    team_date_rows: list[dict[str, object]] = []
    team_a_probability = 0.0
    team_b_probability = 0.0
    games_played = team_a_wins + team_b_wins
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


def _clamp_rate(value: float) -> float:
    return min(0.999, max(0.001, float(value)))


def _binomial_coefficient(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return comb(n, k)
