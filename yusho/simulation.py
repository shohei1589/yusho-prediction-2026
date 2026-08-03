from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import random

import pandas as pd

from .teams import league_teams


@dataclass(frozen=True)
class SimulationResult:
    target_team: str
    champion_probability: float
    champion_dates: pd.DataFrame
    final_standings: pd.DataFrame
    no_champion_count: int


def odds_ratio(p_a: float, p_b: float) -> float:
    denom = p_a * (1 - p_b) + (1 - p_a) * p_b
    if denom == 0:
        return 0.5
    return (p_a * (1 - p_b)) / denom


def run_simulations(
    daily_opponents: pd.DataFrame,
    standings: pd.DataFrame,
    league: str,
    target_team: str,
    simulation_count: int = 10_000,
    seed: int | None = None,
    assumed_win_rates: dict[str, float] | None = None,
    external_win_rates: dict[str, float] | None = None,
) -> SimulationResult:
    teams = league_teams(league)
    if target_team not in teams:
        raise ValueError(f"{target_team} is not in {league}")
    rng = random.Random(seed)

    initial = {
        row.Team: {"Wins": int(row.Wins), "Losses": int(row.Losses), "Ties": int(row.Ties)}
        for row in standings.itertuples(index=False)
    }
    fixed_win_rates = _fixed_win_rates(initial, teams, assumed_win_rates)
    total_games = _total_games(daily_opponents, initial, teams)

    champion_dates: list[dict[str, object]] = []
    final_rows: list[dict[str, object]] = []
    no_champion_count = 0

    for _ in range(simulation_count):
        champion_date, final_standings = simulate_season(
            daily_opponents,
            initial,
            fixed_win_rates,
            total_games,
            teams,
            target_team,
            rng,
            external_win_rates or {},
        )
        if champion_date is None:
            no_champion_count += 1
        else:
            champion_dates.append(champion_date)
        for team, values in final_standings.items():
            final_rows.append({"Team": team, **values})

    probability = len(champion_dates) / simulation_count
    date_counts = _champion_date_counts(champion_dates, probability)
    final_frame = pd.DataFrame(final_rows)
    if not final_frame.empty:
        final_frame = (
            final_frame.groupby("Team", as_index=False)[
                ["Wins", "Losses", "Ties"]
            ]
            .mean()
        )
        final_frame["_WinRate"] = final_frame["Wins"] / (
            final_frame["Wins"] + final_frame["Losses"]
        ).replace(0, 1)
        final_frame = (
            final_frame.sort_values(
                ["_WinRate", "Wins", "Losses"],
                ascending=[False, False, True],
            )
            .drop(columns=["_WinRate"])
        )
        leader_run_diff = final_frame.iloc[0]["Wins"] - final_frame.iloc[0]["Losses"]
        final_frame["GamesBehind"] = (
            leader_run_diff - (final_frame["Wins"] - final_frame["Losses"])
        ) / 2

    return SimulationResult(
        target_team=target_team,
        champion_probability=probability,
        champion_dates=date_counts,
        final_standings=final_frame,
        no_champion_count=no_champion_count,
    )


def simulate_season(
    daily_opponents: pd.DataFrame,
    initial_standings: dict[str, dict[str, int]],
    fixed_win_rates: dict[str, float],
    total_games: dict[str, int],
    teams: tuple[str, ...],
    target_team: str,
    rng: random.Random,
    external_win_rates: dict[str, float] | None = None,
) -> tuple[dict[str, object] | None, dict[str, dict[str, int]]]:
    standings = {
        team: {
            "Wins": values["Wins"],
            "Losses": values["Losses"],
            "Ties": values["Ties"],
        }
        for team, values in initial_standings.items()
    }
    champion_date: dict[str, object] | None = None

    for row in daily_opponents.itertuples(index=False):
        game_date = row.Date
        game_date_label = getattr(row, "DateLabel", "")
        if pd.isna(game_date_label):
            game_date_label = ""
        daily_results = {team: None for team in teams}
        processed_pairs: set[frozenset[str]] = set()

        ordered_teams = sorted(teams, key=lambda team: fixed_win_rates[team], reverse=True)
        for team in ordered_teams:
            opponent = getattr(row, f"{team}_Opponent", pd.NA)
            if pd.isna(opponent):
                continue
            opponent = str(opponent)
            if opponent not in teams:
                if daily_results[team] is None:
                    opponent_rate = (external_win_rates or {}).get(opponent)
                    if opponent_rate is not None:
                        team_win_prob = odds_ratio(
                            fixed_win_rates[team],
                            min(0.999, max(0.001, float(opponent_rate))),
                        )
                    else:
                        team_win_prob = fixed_win_rates[team]
                    daily_results[team] = (
                        "Win" if rng.random() < team_win_prob else "Lose"
                    )
                continue
            pair = frozenset((team, opponent))
            if pair in processed_pairs:
                continue
            processed_pairs.add(pair)

            team_win_prob = odds_ratio(fixed_win_rates[team], fixed_win_rates[opponent])
            if rng.random() < team_win_prob:
                daily_results[team] = "Win"
                daily_results[opponent] = "Lose"
            else:
                daily_results[team] = "Lose"
                daily_results[opponent] = "Win"

        for team, result in daily_results.items():
            if result == "Win":
                standings[team]["Wins"] += 1
            elif result == "Lose":
                standings[team]["Losses"] += 1

        if champion_date is None and _is_championship_decided(
            standings, total_games, teams, target_team
        ):
            champion_date = {
                "Date": pd.Timestamp(game_date),
                "DateLabel": str(game_date_label).strip(),
            }

    return champion_date, standings


def _current_win_rate(wins: int, losses: int) -> float:
    games = wins + losses
    return wins / games if games else 0.5


def _fixed_win_rates(
    initial: dict[str, dict[str, int]],
    teams: tuple[str, ...],
    assumed_win_rates: dict[str, float] | None,
) -> dict[str, float]:
    rates: dict[str, float] = {}
    for team in teams:
        if assumed_win_rates and team in assumed_win_rates:
            rates[team] = min(0.999, max(0.001, float(assumed_win_rates[team])))
        else:
            rates[team] = _current_win_rate(initial[team]["Wins"], initial[team]["Losses"])
    return rates


def _total_games(
    daily_opponents: pd.DataFrame,
    initial: dict[str, dict[str, int]],
    teams: tuple[str, ...],
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for team in teams:
        played = initial[team]["Wins"] + initial[team]["Losses"] + initial[team]["Ties"]
        remaining = daily_opponents[f"{team}_Opponent"].notna().sum()
        totals[team] = int(played + remaining)
    return totals


def _is_championship_decided(
    standings: dict[str, dict[str, int]],
    total_games: dict[str, int],
    teams: tuple[str, ...],
    target_team: str,
) -> bool:
    target = standings[target_team]
    target_remaining = _remaining_games(standings, total_games, target_team)
    target_floor = target["Wins"] / (
        target["Wins"] + target["Losses"] + target_remaining
    )

    for team in teams:
        if team == target_team:
            continue
        team_remaining = _remaining_games(standings, total_games, team)
        challenger = standings[team]
        challenger_ceiling = (challenger["Wins"] + team_remaining) / (
            challenger["Wins"] + challenger["Losses"] + team_remaining
        )
        if target_floor <= challenger_ceiling:
            return False
    return True


def _remaining_games(
    standings: dict[str, dict[str, int]],
    total_games: dict[str, int],
    team: str,
) -> int:
    values = standings[team]
    played = values["Wins"] + values["Losses"] + values["Ties"]
    return max(0, total_games[team] - played)


def _champion_date_counts(
    champion_dates: list[dict[str, object]],
    champion_probability: float,
) -> pd.DataFrame:
    if not champion_dates:
        return pd.DataFrame(columns=["Date", "DateLabel", "Probability"])

    frame = pd.DataFrame(champion_dates)
    frame["Date"] = pd.to_datetime(frame["Date"])
    if "DateLabel" not in frame.columns:
        frame["DateLabel"] = ""
    frame["DateLabel"] = frame["DateLabel"].fillna("").astype(str).str.strip()
    labeled = frame[frame["DateLabel"] != ""]
    regular = frame[frame["DateLabel"] == ""]

    count_frames: list[pd.DataFrame] = []
    if not regular.empty:
        count_frames.append(
            regular.groupby(["Date", "DateLabel"], dropna=False)
            .size()
            .reset_index(name="Count")
        )
    if not labeled.empty:
        count_frames.append(
            labeled.groupby("DateLabel", as_index=False)
            .agg(Date=("Date", "max"), Count=("Date", "size"))
            .loc[:, ["Date", "DateLabel", "Count"]]
        )
    counts = pd.concat(count_frames, ignore_index=True)
    counts = counts.sort_values("Date")
    counts["Probability"] = counts["Count"] / len(champion_dates) * champion_probability
    return counts[["Date", "DateLabel", "Probability"]].reset_index(drop=True)
