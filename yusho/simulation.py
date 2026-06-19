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

    champion_dates: list[pd.Timestamp] = []
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
            final_frame.groupby("Team", as_index=False)[["Wins", "Losses", "Ties"]]
            .mean()
            .sort_values(["Wins", "Losses"], ascending=[False, True])
        )

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
) -> tuple[pd.Timestamp | None, dict[str, dict[str, int]]]:
    standings = {
        team: {
            "Wins": values["Wins"],
            "Losses": values["Losses"],
            "Ties": values["Ties"],
        }
        for team, values in initial_standings.items()
    }

    for row in daily_opponents.itertuples(index=False):
        game_date = row.Date
        daily_results = {team: None for team in teams}
        processed_pairs: set[frozenset[str]] = set()

        ordered_teams = sorted(teams, key=lambda team: fixed_win_rates[team], reverse=True)
        for team in ordered_teams:
            opponent = getattr(row, f"{team}_Opponent", pd.NA)
            if pd.isna(opponent):
                continue
            opponent = str(opponent)
            if opponent not in teams:
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

        if _is_championship_decided(standings, total_games, teams, target_team):
            return pd.Timestamp(game_date), standings

    return None, standings


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
    champion_dates: list[pd.Timestamp],
    champion_probability: float,
) -> pd.DataFrame:
    if not champion_dates:
        return pd.DataFrame(columns=["Date", "Probability"])

    counts = pd.Series(champion_dates).value_counts(normalize=True).reset_index()
    counts.columns = ["Date", "Probability"]
    counts["Date"] = pd.to_datetime(counts["Date"])
    counts = counts.sort_values("Date")
    counts["Probability"] = counts["Probability"] * champion_probability
    return counts.reset_index(drop=True)
