from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .teams import league_teams


@dataclass(frozen=True)
class MagicAnalysis:
    required_wins: int | None
    is_clinched: bool
    is_lit: bool
    clinch_table: pd.DataFrame
    lighting_table: pd.DataFrame


def analyze_magic(
    standings: pd.DataFrame,
    schedule: pd.DataFrame,
    league: str,
    target_team: str,
) -> MagicAnalysis:
    teams = league_teams(league)
    records = {
        str(row.Team): {
            "Wins": int(row.Wins),
            "Losses": int(row.Losses),
            "Ties": int(row.Ties),
        }
        for row in standings.itertuples(index=False)
    }
    remaining = _remaining_by_team(schedule, teams)
    target_remaining = remaining[target_team]
    target_record = records[target_team]

    clinch_rows: list[dict[str, object]] = []
    lighting_rows: list[dict[str, object]] = []

    for rival in teams:
        if rival == target_team:
            continue
        direct_remaining = _direct_remaining(schedule, target_team, rival)
        rival_record = records[rival]
        rival_remaining = remaining[rival]
        needed_wins, target_rate, rival_rate = _needed_wins_vs_rival(
            target_record,
            rival_record,
            target_remaining,
            rival_remaining,
            direct_remaining,
        )
        lit_target_rate, lit_rival_rate, is_lit_vs_rival = _lighting_check_vs_rival(
            target_record,
            rival_record,
            target_remaining,
            rival_remaining,
            direct_remaining,
        )

        clinch_rows.append(
            {
                "Team": rival,
                "TargetRemaining": target_remaining,
                "RivalRemaining": rival_remaining,
                "DirectRemaining": direct_remaining,
                "NeededWins": needed_wins,
                "TargetRate": target_rate,
                "RivalMaxRate": rival_rate,
            }
        )
        lighting_rows.append(
            {
                "Team": rival,
                "DirectRemaining": direct_remaining,
                "TargetScenarioRate": lit_target_rate,
                "RivalMaxRate": lit_rival_rate,
                "IsLit": is_lit_vs_rival,
            }
        )

    clinch_table = pd.DataFrame(clinch_rows)
    lighting_table = pd.DataFrame(lighting_rows)
    required_wins = _overall_required_wins(clinch_table)
    is_clinched = required_wins == 0
    is_lit = bool(lighting_table["IsLit"].all()) if not lighting_table.empty else False

    return MagicAnalysis(
        required_wins=required_wins,
        is_clinched=is_clinched,
        is_lit=is_lit,
        clinch_table=clinch_table,
        lighting_table=lighting_table,
    )


def _remaining_by_team(schedule: pd.DataFrame, teams: tuple[str, ...]) -> dict[str, int]:
    remaining: dict[str, int] = {}
    for team in teams:
        if schedule.empty:
            remaining[team] = 0
        else:
            remaining[team] = int(
                ((schedule["HomeTeam"] == team) | (schedule["AwayTeam"] == team)).sum()
            )
    return remaining


def _direct_remaining(schedule: pd.DataFrame, target_team: str, rival: str) -> int:
    if schedule.empty:
        return 0
    return int(
        (
            ((schedule["HomeTeam"] == target_team) & (schedule["AwayTeam"] == rival))
            | ((schedule["HomeTeam"] == rival) & (schedule["AwayTeam"] == target_team))
        ).sum()
    )


def _needed_wins_vs_rival(
    target: dict[str, int],
    rival: dict[str, int],
    target_remaining: int,
    rival_remaining: int,
    direct_remaining: int,
) -> tuple[int | None, float, float]:
    target_non_direct_remaining = max(0, target_remaining - direct_remaining)
    best_target_rate = 0.0
    best_rival_rate = 1.0

    for target_wins_needed in range(target_remaining + 1):
        forced_direct_wins = max(0, target_wins_needed - target_non_direct_remaining)
        rival_wins = rival["Wins"] + rival_remaining - forced_direct_wins
        rival_losses = rival["Losses"] + forced_direct_wins
        target_wins = target["Wins"] + target_wins_needed
        target_losses = target["Losses"] + target_remaining - target_wins_needed
        target_rate = _win_rate(target_wins, target_losses)
        rival_rate = _win_rate(rival_wins, rival_losses)
        best_target_rate = target_rate
        best_rival_rate = rival_rate
        if target_rate > rival_rate:
            return target_wins_needed, target_rate, rival_rate

    return None, best_target_rate, best_rival_rate


def _lighting_check_vs_rival(
    target: dict[str, int],
    rival: dict[str, int],
    target_remaining: int,
    rival_remaining: int,
    direct_remaining: int,
) -> tuple[float, float, bool]:
    target_wins = target["Wins"] + max(0, target_remaining - direct_remaining)
    target_losses = target["Losses"] + direct_remaining
    rival_wins = rival["Wins"] + rival_remaining
    rival_losses = rival["Losses"]
    target_rate = _win_rate(target_wins, target_losses)
    rival_rate = _win_rate(rival_wins, rival_losses)
    return target_rate, rival_rate, target_rate > rival_rate


def _overall_required_wins(clinch_table: pd.DataFrame) -> int | None:
    if clinch_table.empty or clinch_table["NeededWins"].isna().any():
        return None
    return int(clinch_table["NeededWins"].max())


def _win_rate(wins: int, losses: int) -> float:
    decisions = wins + losses
    return wins / decisions if decisions else 0.0
