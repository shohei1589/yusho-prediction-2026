from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import random

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


def run_japan_series_simulations(
    team_a: str,
    team_b: str,
    start_date: date,
    team_a_win_rate: float = 0.5,
    team_b_win_rate: float = 0.5,
    simulation_count: int = 10_000,
    seed: int | None = None,
) -> JapanSeriesResult:
    """Simulate a best-of-seven Japan Series and its decision date."""
    if team_a == team_b:
        raise ValueError("代表チームは異なるチームを指定してください")
    if simulation_count < 1:
        raise ValueError("simulation_count must be positive")

    team_a_win_rate = _clamp_rate(team_a_win_rate)
    team_b_win_rate = _clamp_rate(team_b_win_rate)
    game_win_probability = odds_ratio(team_a_win_rate, team_b_win_rate)
    schedule = japan_series_schedule(start_date)
    rng = random.Random(seed)

    champion_rows: list[dict[str, object]] = []
    team_date_rows: list[dict[str, object]] = []
    team_a_wins = 0

    for _ in range(simulation_count):
        wins_a = 0
        wins_b = 0
        for row in schedule.itertuples(index=False):
            if rng.random() < game_win_probability:
                wins_a += 1
                winner = team_a
            else:
                wins_b += 1
                winner = team_b
            if wins_a == 4 or wins_b == 4:
                champion_date = pd.Timestamp(row.Date)
                champion_rows.append(
                    {
                        "Date": champion_date,
                        "DateLabel": f"{champion_date.month}/{champion_date.day}",
                        "Winner": winner,
                    }
                )
                team_date_rows.append(
                    {
                        "Date": champion_date,
                        "Winner": team_a if wins_a == 4 else team_b,
                    }
                )
                if wins_a == 4:
                    team_a_wins += 1
                break

    champion_frame = pd.DataFrame(champion_rows)
    if champion_frame.empty:
        champion_dates = pd.DataFrame(columns=["Date", "DateLabel", "Probability"])
    else:
        champion_dates = (
            champion_frame.groupby(["Date", "DateLabel"], as_index=False)
            .size()
            .rename(columns={"size": "Probability"})
        )
        champion_dates["Probability"] /= simulation_count
        champion_dates = champion_dates.sort_values("Date").reset_index(drop=True)

    team_date_frame = pd.DataFrame(team_date_rows)
    if team_date_frame.empty:
        team_date_probabilities = pd.DataFrame(
            columns=["Date", "Winner", "Probability"]
        )
    else:
        team_date_probabilities = (
            team_date_frame.groupby(["Date", "Winner"], as_index=False)
            .size()
            .rename(columns={"size": "Probability"})
        )
        team_date_probabilities["Probability"] /= simulation_count
        team_date_probabilities = team_date_probabilities.sort_values(
            ["Date", "Winner"]
        ).reset_index(drop=True)

    return JapanSeriesResult(
        team_a=team_a,
        team_b=team_b,
        team_a_probability=team_a_wins / simulation_count,
        team_b_probability=1 - (team_a_wins / simulation_count),
        champion_dates=champion_dates,
        team_date_probabilities=team_date_probabilities,
        series_schedule=schedule,
    )


def _clamp_rate(value: float) -> float:
    return min(0.999, max(0.001, float(value)))
