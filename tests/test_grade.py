"""grade_match: each player's grade from the match's outcomes and scores, on matches as the protocol gives them and
against the served example games."""

import pytest
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score
from conftest import deployed
from test_match import Race
from test_steps import add, opening
from tictactoe import TicTacToe

from agentenv_game.grade import GradeMatchTaskStep, share
from agentenv_game.steps import CloseLobbyTaskStep, FinishMatchTaskStep


def grader(**more):
    return GradeMatchTaskStep(id="grade", version=None, env_id="tictactoe", **more)


def lobby(*slots, teams=None):
    """A closed lobby of (player_id, player_kind) slots, each its own team unless `teams` groups them."""
    teams = teams or [[p] for p, _ in slots]
    return {"lobby_id": "lb-1", "status": "closed",
            "player_slots": [{"player_id": p, "player_kind": k} for p, k in slots],
            "player_teams": [{"team_id": f"t{i}", "player_ids": ids} for i, ids in enumerate(teams)]}


def match(status="finished", detail="the time limit", **states):
    return {"lobby_id": "lb-1", "status": status, "status_detail": detail,
            "progress": [{"name": "game", "unit": "seconds", "value": 300, "limit": 300}], "player_states": states}


def state(status, better="higher", **scores):
    return {"status": status, "scores": [{"name": n, "value": v, "better": better} for n, v in scores.items()]}


def score_of(rows):
    return aggregate_score(rows, ScoreAggregator.WEIGHTED_AVERAGE)


DUEL = lobby(("0", "agent"), ("1", "ai"))
OUTCOME_CRITERION = "the outcome: a win 1, a draw 0.5, a loss 0; undecided at the match's end is a draw"


def test_the_outcome_alone_by_default_a_win_1_a_draw_half_a_loss_0():
    graded = grader().grade(match(**{"0": state("won", score=900, army=10), "1": state("lost", score=300, army=0)}),
                            DUEL)
    assert list(graded) == ["grade"]
    outcome, score, army = graded["grade"]
    assert outcome == {"name": "outcome", "result": True, "score": 1.0, "weight": 1,
                       "evidence": "won (the time limit, at 300 of 300 seconds)",
                       "criterion": OUTCOME_CRITERION}
    assert score == {"name": "score", "result": None, "weight": 0, "evidence": "900 vs 300, 75% of the two",
                     "criterion": "score against the best opponent's, as a share of the two (information only)"}
    assert army["evidence"] == "10 vs 0, 100% of the two" and score_of(graded["grade"]) == 1.0
    for status, points in (("drawn", 0.5), ("undecided", 0.5), ("lost", 0.0)):
        rows = grader().grade(match(**{"0": state(status), "1": state("lost")}), DUEL)["grade"]
        assert score_of(rows) == points and rows[0]["result"] is False
    assert grader().grade(match(**{"0": state("undecided"), "1": state("undecided")}), DUEL)["grade"][0][
        "evidence"] == "a draw: the match ended without deciding this player (the time limit, at 300 of 300 seconds)"


def test_a_weighted_score_counts_as_its_share_against_the_best_opponent():
    everyone = lobby(("0", "agent"), ("1", "ai"), ("2", "ai"))
    states = {"0": state("lost", score=300, army=5), "1": state("lost", score=100, army=9),
              "2": state("won", score=200)}
    rows = grader(weights={"score": 1}).grade(match(**states), everyone)["grade"]
    assert [(r["name"], r["weight"], r.get("score")) for r in rows] == [("outcome", 1, 0.0), ("score", 1, 0.6),
                                                                       ("army", 0, None)]
    assert rows[1]["result"] is True and rows[1]["evidence"] == "300 vs 200, 60% of the two"
    assert rows[2]["evidence"] == "5 vs 9, 36% of the two" and score_of(rows) == pytest.approx(0.3)
    alone = grader(weights={"outcome": 0, "army": 2}).grade(match(**states), everyone)["grade"]
    assert [(r["name"], r["result"]) for r in alone] == [("outcome", None), ("score", None), ("army", False)]
    assert score_of(alone) == pytest.approx(5 / 14)


def test_allies_are_not_opponents_and_lower_can_be_better():
    teams = lobby(("0", "agent"), ("1", "agent"), ("2", "ai"), teams=[["0", "1"], ["2"]])
    states = {"0": state("won", score=50), "1": state("won", score=500), "2": state("lost", score=100)}
    rows = grader(player_ids=["0"], weights={"score": 1}).grade(match(**states), teams)["grade:0"]
    assert rows[1]["score"] == pytest.approx(1 / 3)
    deaths = {"0": state("won", "lower", deaths=2), "1": state("won", "lower", deaths=0),
              "2": state("lost", "lower", deaths=4)}
    rows = grader(player_ids=["0"], weights={"deaths": 1}).grade(match(**deaths), teams)["grade:0"]
    assert rows[1]["score"] == pytest.approx(2 / 3) and rows[1]["evidence"] == (
        "2 vs 4, 67% of the two (lower is better)")
    assert share(0, 0, "higher") == 0.5 and share(-1, -10, "higher") == pytest.approx(0.5 + 9 / 22)
    assert share(5, -5, "higher") == 1.0 and share(5, -5, "lower") == 0.0


def test_each_graded_player_gets_its_own_verification():
    two = lobby(("0", "agent"), ("1", "agent"), ("2", "ai"))
    states = {"0": state("won"), "1": state("lost"), "2": state("lost")}
    graded = grader().grade(match(**states), two)
    assert {k: score_of(v) for k, v in graded.items()} == {"grade:0": 1.0, "grade:1": 0.0}
    assert list(grader(player_ids=["2"], verifier_id="duel").grade(match(**states), two)) == ["duel:2"]
    with pytest.raises(RuntimeError, match="no player slot '7'; its player slots are '0', '1', '2'"):
        grader(player_ids=["7"]).grade(match(**states), two)
    with pytest.raises(RuntimeError, match="no agent plays the match"):
        grader().grade(match(**states), lobby(("0", "ai"), ("1", "ai"), ("2", "ai")))


def test_a_score_the_player_or_its_opponents_dont_report_counts_0():
    states = {"0": state("won", score=10), "1": {"status": "lost"}}
    [_, missing] = grader(weights={"score": 1}).grade(match(**states), DUEL)["grade"]
    assert (missing["result"], missing["score"], missing["evidence"]) == (False, 0.0, "no opponent reports it")
    states = {"0": {"status": "won"}, "1": state("lost", score=10)}
    assert grader(weights={"score": 1}).grade(match(**states), DUEL)["grade"][1][
        "evidence"] == "this player doesn't report it"
    solo = grader(weights={"score": 1}).grade(match(**{"0": state("won", score=10)}), lobby(("0", "agent")))
    assert solo["grade"][1]["evidence"] == "no opponent to compare with"
    with pytest.raises(RuntimeError, match="weighs kills, which the game doesn't report: its scores are score"):
        grader(weights={"kills": 1}).grade(match(**states), DUEL)


def test_a_game_that_counts_moves_gates_the_grade_on_them():
    states = {"0": {**state("won"), "moves": {"made": 0, "missed": 0}}, "1": state("lost")}
    rows = grader().grade(match(**states), DUEL)["grade"]
    assert rows[-1] == {"name": "played", "criterion": "the player made a move", "result": False, "weight": -100,
                        "evidence": "made 0, missed 0"} and score_of(rows) == 0.0
    states["0"]["moves"] = {"made": 9, "missed": 1}
    rows = grader(max_missed=0.1).grade(match(**states), DUEL)["grade"]
    assert [(r["name"], r["result"]) for r in rows[-2:]] == [("played", True), ("missed", True)]
    assert rows[-1]["criterion"] == "missed at most 10% of its moves" and score_of(rows) == 1.0
    states["0"]["moves"] = {"made": 8, "missed": 2}
    assert score_of(grader(max_missed=0.1).grade(match(**states), DUEL)["grade"]) == 0.0
    with pytest.raises(RuntimeError, match="max_missed needs the game's count of moves"):
        grader(max_missed=0.1).grade(match(**{"0": state("won"), "1": state("lost")}), DUEL)
    for given in (1.5, -0.1, True, "0.1"):
        with pytest.raises(ValueError, match="max_missed is a share"):
            grader(max_missed=given)


def test_a_match_that_didnt_finish_has_no_grade():
    states = {"0": state("undecided"), "1": state("undecided")}
    with pytest.raises(RuntimeError, match="no outcome to grade, the match cancelled: the harness ended it"):
        grader().grade(match("cancelled", "the harness ended it", **states), DUEL)
    with pytest.raises(RuntimeError, match="no outcome to grade, the match failed: wine crashed"):
        grader().grade(match("failed", "wine crashed", **states), DUEL)
    with pytest.raises(RuntimeError, match="the match is still started; grade it after finish_match"):
        grader().grade(match("started", None, **states), DUEL)


def test_the_step_loads_from_task_json_and_checks_its_fields():
    step = get_task_step_registry()["grade_match"].from_dict(
        {"id": "grade", "type": "grade_match", "env_id": "wc3", "player_ids": ["0"], "weights": {"score": 0.5},
         "depends_on": ["finish"]})
    assert {k: step.to_dict()[k] for k in ("env_id", "player_ids", "weights", "max_missed", "verifier_id",
                                          "timeout_seconds")} == {
        "env_id": "wc3", "player_ids": ["0"], "weights": {"score": 0.5}, "max_missed": None, "verifier_id": "grade",
        "timeout_seconds": 60}
    assert step.criteria() == {"outcome": 1, "score": 0.5} and step.entity_refs and step.fail_task_on_error
    with pytest.raises(ValueError, match="needs a criterion with a weight above 0"):
        grader(weights={"outcome": 0})
    for weights in ({"score": "1"}, {"score": True}, ["score"]):
        with pytest.raises(ValueError, match="weights maps each criterion"):
            grader(weights=weights)
    for player_ids in ([], "0", [0]):
        with pytest.raises(ValueError, match="player_ids is a list"):
            grader(player_ids=player_ids)



@pytest.mark.anyio
async def test_grade_match_grades_the_served_match_once_it_is_over():
    game = TicTacToe()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        await opening().execute(context)
        await add("slot-x", "x", "agent", "alice", register=False).execute(context)
        await add("slot-o", "o", "ai").execute(context)
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
        with pytest.raises(RuntimeError, match="the match is still started"):
            await grader().execute(context)
        for cell in (0, 3, 6):
            game._put(cell)
            game._ai_moves()
        await grader().execute(context)
    assert context.metadata["verifications"]["grade"] == {"score": 1.0, "results": [
        {"name": "outcome", "criterion": OUTCOME_CRITERION, "result": True, "score": 1.0,
         "evidence": "won (x won, at 5 of 9 moves)", "weight": 1}]}
    assert context.metadata["game_match"]["status"] == "finished"


@pytest.mark.anyio
async def test_grade_match_after_finish_match_with_a_weighted_score_and_only_the_runs_match():
    game = Race()
    async with deployed(game) as env:
        context = TaskStepContext(deployed_envs=[env])
        await opening().execute(context)
        await add("slot-a", "a", "agent", "alice", register=False).execute(context)
        await add("slot-c", "c", "ai").execute(context)
        await CloseLobbyTaskStep(id="close", version=None, env_id="tictactoe").execute(context)
        await game.step("a")
        await game.step("a")
        await FinishMatchTaskStep(id="finish", version=None, env_id="tictactoe").execute(context)
        await grader(weights={"distance": 1}).execute(context)
        await GradeMatchTaskStep(id="strict", version=None, env_id="tictactoe", max_missed=0.5).execute(context)
        ours = context.metadata["game_lobby"]["lobby_id"]
        context.metadata["game_lobby"] = {"lobby_id": "lb-elsewhere"}
        with pytest.raises(RuntimeError, match=f"match is lobby {ours}'s, not this run's, lb-elsewhere"):
            await grader().execute(context)
        game.new_lobby()
        with pytest.raises(RuntimeError, match="has no match; grade it after close_lobby and finish_match"):
            await grader().execute(context)
    [outcome, distance, played] = context.metadata["verifications"]["grade"]["results"]
    assert outcome["evidence"] == "lost (c won, at 6 of 10 ticks)"
    assert (distance["score"], distance["evidence"]) == (0.4, "2 vs 3, 40% of the two")
    assert (played["result"], played["weight"], played["evidence"]) == (True, -100, "made 2, missed 6")
    assert context.metadata["verifications"]["grade"]["score"] == pytest.approx(0.2)
    missed = context.metadata["verifications"]["strict"]["results"][-1]
    assert (missed["name"], missed["result"], missed["evidence"]) == ("missed", False, "missed 6 of 8 (75%)")
    assert context.metadata["verifications"]["strict"]["score"] == 0.0
