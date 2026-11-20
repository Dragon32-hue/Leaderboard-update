import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import leaderboard as lb


def make_clan(tag="RED", slug="red", name="Red Clan"):
    return lb.Clan(name=f"[{tag}] {name}", tag=tag, color=0xFF0000, members=3, slug=slug)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"ok"


class TestParseLevels(unittest.TestCase):
    def test_parses_levels_and_users(self):
        response = "1:409:2:Level One:6:127|1:410:2:Level Two:6:128#127:Astro|128:Bboppa#"
        levels, users = lb.parse_levels(response)
        self.assertEqual(len(levels), 2)
        self.assertEqual(levels[0]["1"], "409")
        self.assertEqual(levels[1]["2"], "Level Two")
        self.assertEqual(users, {"127": "Astro", "128": "Bboppa"})

    def test_strips_account_id_from_username(self):
        response = "1:409:2:Level One:6:127#127:Astro:125|128:Bboppa:206#"
        _, users = lb.parse_levels(response)
        self.assertEqual(users, {"127": "Astro", "128": "Bboppa"})

    def test_empty_and_malformed_responses(self):
        self.assertEqual(lb.parse_levels("-1"), ([], {}))
        self.assertEqual(lb.parse_levels("#"), ([], {}))
        levels, _ = lb.parse_levels("garbage|1:7:2:Ok#")
        self.assertEqual(len(levels), 1)
        self.assertEqual(levels[0]["1"], "7")


class TestParseClans(unittest.TestCase):
    def test_parses_cards_with_tag_members_and_slug(self):
        html = """
        <div class="profile clanscard">
          <span style="color:#ff0000">[RED] Red &amp; Blue</span>
          <a href="/dashboard/clan/red_clan">link</a>
          <b>7</b> members
        </div>
        <div class="profile clanscard">
          <span style="color:#00FF00">[GREEN] Green</span>
          <a href="/clan/green">link</a>
        </div>
        """
        clans = lb.parse_clans(html)
        self.assertEqual(len(clans), 2)
        self.assertEqual(clans[0].name, "[RED] Red & Blue")
        self.assertEqual(clans[0].tag, "RED")
        self.assertEqual(clans[0].color, 0xFF0000)
        self.assertEqual(clans[0].members, 7)
        self.assertEqual(clans[0].slug, "red_clan")
        self.assertEqual(clans[1].tag, "GREEN")
        self.assertEqual(clans[1].members, 0)

    def test_parses_card_with_nested_icon_in_name(self):
        html = """
        <div class="profile clanscard">
          <span style="color:#f0d000;display: inline-flex;"> [YELLO] Yellow <i class="fa-solid fa-lock"></i></span>
          <button onclick="a('clan/Yellow', true, true)">view</button>
          <b>19</b> members
        </div>
        """
        clans = lb.parse_clans(html)
        self.assertEqual(len(clans), 1)
        self.assertEqual(clans[0].name, "[YELLO] Yellow")
        self.assertEqual(clans[0].tag, "YELLO")
        self.assertEqual(clans[0].slug, "Yellow")
        self.assertEqual(clans[0].members, 19)
        self.assertEqual(clans[0].color, 0xF0D000)

    def test_no_cards_returns_empty(self):
        self.assertEqual(lb.parse_clans("<html></html>"), [])


class TestParseRoster(unittest.TestCase):
    def test_parses_members_and_owner(self):
        html = """
        <div class="profilenick clanmembernick extra"><img src="x">[TAG] Alice</div>
        <div class="clanownernick owner"><img src="x">Bob</div>
        """
        self.assertEqual(lb.parse_roster(html), {"[TAG] Alice", "Bob"})

    def test_build_user_clan_map_normalizes_case_and_tags(self):
        clans = [make_clan()]
        rosters = {"red": {"[X] Alice", "bob"}}
        mapping = lb.build_user_clan_map(clans, rosters)
        self.assertEqual(mapping, {"alice": "RED", "bob": "RED"})


class TestCleanUsername(unittest.TestCase):
    def test_removes_leading_tag_and_whitespace(self):
        self.assertEqual(lb.clean_username("  [RED] Alice  "), "Alice")
        self.assertEqual(lb.clean_username("[A] [B] C"), "[B] C")
        self.assertEqual(lb.clean_username("Bob"), "Bob")


class TestState(unittest.TestCase):
    def test_round_trip(self):
        state = lb.State(
            points={"RED": 5},
            levels={"1": lb.LevelRecord("epic", "7", "Alice", "RED")},
            pending=lb.PendingPost(payload={"content": "x"}, created_at="2026-01-01T00:00:00Z"),
        )
        restored = lb.State.from_json(json.loads(json.dumps(state.to_json())))
        self.assertEqual(restored.points, {"RED": 5})
        self.assertEqual(restored.levels["1"], state.levels["1"])
        self.assertIsNotNone(restored.pending)
        self.assertEqual(restored.pending.payload, {"content": "x"})

    def test_from_json_skips_bad_entries(self):
        raw = {
            "points": {"RED": 5, "BAD": "nope"},
            "levels": {
                "1": {"tier": "star", "uid": "1", "user": "A"},
                "2": "broken",
                "3": {"tier": "unknown", "uid": "3", "user": "B"},
            },
        }
        state = lb.State.from_json(raw)
        self.assertEqual(state.points, {"RED": 5})
        self.assertEqual(list(state.levels), ["1"])
        self.assertIsNone(state.levels["1"].clan)

    def test_from_json_rejects_non_object_root(self):
        with self.assertRaises(lb.StateError):
            lb.State.from_json([])

    def test_corrupt_file_is_backed_up_and_baseline_used(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            state = lb.load_state(path)
            self.assertEqual(state.points, lb.BASELINE)
            backups = list(Path(directory).glob("state.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")

    def test_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            state = lb.State(points={"RED": 1}, levels={})
            lb.save_state(path, state)
            self.assertFalse(path.with_name("state.json.tmp").exists())
            self.assertEqual(lb.load_state(path).points, {"RED": 1})


class TestComputeUpdate(unittest.TestCase):
    TAGS = ["RED", "GREEN"]

    def test_new_rating_adds_points_to_matching_clan(self):
        state = lb.State(points={"RED": 10, "GREEN": 20}, levels={})
        tiers = {"1": lb.Level("1", "100", "epic", "First")}
        points, levels, net, changes = lb.compute_update(
            tiers,
            {"100": "[X] Alice"},
            {"alice": "RED", "bob": "GREEN"},
            self.TAGS,
            state,
        )
        self.assertEqual(points, {"RED": 20, "GREEN": 20})
        self.assertEqual(net, {"RED": 10, "GREEN": 0})
        self.assertEqual(levels["1"].clan, "RED")
        self.assertEqual(levels["1"].user, "Alice")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].points, 10)
        self.assertEqual(changes[0].label, "rated epic")

    def test_upgrade_and_demotion_use_the_difference(self):
        state = lb.State(
            points={"RED": 10},
            levels={"1": lb.LevelRecord("epic", "100", "Alice", "RED")},
        )
        upgrade = lb.compute_update(
            {"1": lb.Level("1", "100", "mythic", "First")},
            {"100": "Alice"},
            {"alice": "RED"},
            self.TAGS,
            state,
        )
        self.assertEqual(upgrade[0]["RED"], 40)
        self.assertEqual(upgrade[2]["RED"], 30)
        self.assertEqual(upgrade[3][0].label, "epic → mythic")

        demotion = lb.compute_update(
            {"1": lb.Level("1", "100", "star", "First")},
            {"100": "Alice"},
            {"alice": "RED"},
            self.TAGS,
            state,
        )
        self.assertEqual(demotion[0]["RED"], 1)
        self.assertEqual(demotion[2]["RED"], -9)
        self.assertEqual(demotion[3][0].tier, "epic")

    def test_removal_subtracts_only_from_recorded_clan(self):
        state = lb.State(
            points={"RED": 40, "GREEN": 0},
            levels={"1": lb.LevelRecord("mythic", "100", "Alice", "RED", 40)},
        )
        points, levels, net, changes = lb.compute_update(
            {}, {}, {"alice": "GREEN"}, self.TAGS, state
        )
        self.assertEqual(points, {"RED": 0, "GREEN": 0})
        self.assertEqual(net, {"RED": -40, "GREEN": 0})
        self.assertEqual(levels, {})
        self.assertEqual(changes[0].points, -40)
        self.assertEqual(changes[0].label, "mythic → unrated")

    def test_unrated_level_with_no_credit_does_not_debit_current_clan(self):
        state = lb.State(
            points={"RED": 10},
            levels={"1": lb.LevelRecord("epic", "100", "Alice")},
        )
        points, _, net, changes = lb.compute_update(
            {}, {}, {"alice": "RED"}, self.TAGS, state
        )
        self.assertEqual(points["RED"], 10)
        self.assertEqual(net["RED"], 0)
        self.assertEqual(changes, [])

    def test_unchanged_legacy_record_keeps_clan_none(self):
        state = lb.State(
            points={"RED": 10},
            levels={"1": lb.LevelRecord("epic", "100", "Alice")},
        )
        points, levels, _, changes = lb.compute_update(
            {"1": lb.Level("1", "100", "epic", "First")},
            {"100": "Alice"},
            {"alice": "RED"},
            self.TAGS,
            state,
        )
        self.assertEqual(points, {"RED": 10})
        self.assertIsNone(levels["1"].clan)
        self.assertEqual(changes, [])

    def test_rerate_then_unrate_debits_only_credited_delta(self):
        state = lb.State(
            points={"RED": 10, "GREEN": 0},
            levels={"1": lb.LevelRecord("epic", "100", "Alice", "RED", 10)},
        )
        points, levels, _, _ = lb.compute_update(
            {"1": lb.Level("1", "100", "legendary", "First")},
            {"100": "Alice"},
            {"alice": "GREEN"},
            self.TAGS,
            state,
        )
        self.assertEqual(points, {"RED": 10, "GREEN": 10})
        self.assertEqual(levels["1"].clan, "GREEN")
        self.assertEqual(levels["1"].credit, 10)

        points, _, net, _ = lb.compute_update({}, {}, {}, self.TAGS, lb.State(points=points, levels=levels))
        self.assertEqual(points, {"RED": 10, "GREEN": 0})
        self.assertEqual(net["GREEN"], -10)

    def test_legacy_state_infers_credit_from_tier(self):
        state = lb.State.from_json(
            {
                "version": 2,
                "points": {"RED": 0},
                "levels": {"1": {"tier": "legendary", "uid": "100", "user": "Alice", "clan": "RED"}},
            }
        )
        self.assertEqual(state.levels["1"].credit, 20)

    def test_clan_switch_does_not_transfer_existing_points(self):
        state = lb.State(
            points={"RED": 10, "NEW": 0},
            levels={"1": lb.LevelRecord("epic", "100", "Alice", "RED")},
        )
        points, levels, net, _ = lb.compute_update(
            {"1": lb.Level("1", "100", "epic", "First")},
            {"100": "Alice"},
            {"alice": "NEW"},
            ["RED", "NEW"],
            state,
        )
        self.assertEqual(points, {"RED": 10, "NEW": 0})
        self.assertEqual(net, {"RED": 0, "NEW": 0})
        self.assertEqual(levels["1"].clan, "RED")

    def test_creator_change_does_not_transfer_credit(self):
        state = lb.State(
            points={"RED": 10, "GREEN": 0},
            levels={"1": lb.LevelRecord("epic", "100", "Alice", "RED")},
        )
        points, levels, net, _ = lb.compute_update(
            {"1": lb.Level("1", "200", "epic", "First")},
            {"200": "Bob"},
            {"bob": "GREEN"},
            self.TAGS,
            state,
        )
        self.assertEqual(points, {"RED": 10, "GREEN": 0})
        self.assertEqual(net, {"RED": 0, "GREEN": 0})
        self.assertEqual(levels["1"].uid, "200")
        self.assertEqual(levels["1"].clan, "GREEN")

    def test_rerate_after_clan_switch_credits_current_clan_only(self):
        state = lb.State(
            points={"RED": 10, "GREEN": 0},
            levels={"1": lb.LevelRecord("epic", "100", "Alice", "RED")},
        )
        points, _, net, _ = lb.compute_update(
            {"1": lb.Level("1", "100", "mythic", "First")},
            {"100": "Alice"},
            {"alice": "GREEN"},
            self.TAGS,
            state,
        )
        self.assertEqual(points, {"RED": 10, "GREEN": 30})
        self.assertEqual(net, {"RED": 0, "GREEN": 30})

    def test_unchanged_level_produces_no_change(self):
        record = lb.LevelRecord("epic", "100", "Alice", "RED")
        state = lb.State(points={"RED": 10}, levels={"1": record})
        points, levels, net, changes = lb.compute_update(
            {"1": lb.Level("1", "100", "epic", "First")},
            {"100": "Alice"},
            {"alice": "RED"},
            self.TAGS,
            state,
        )
        self.assertEqual(points, {"RED": 10})
        self.assertEqual(net, {"RED": 0, "GREEN": 0})
        self.assertEqual(changes, [])
        self.assertEqual(levels["1"], record)

    def test_unknown_user_records_level_without_points(self):
        state = lb.State(points={"RED": 0}, levels={})
        points, levels, _, changes = lb.compute_update(
            {"1": lb.Level("1", "100", "star", "First")},
            {},
            {"alice": "RED"},
            self.TAGS,
            state,
        )
        self.assertEqual(points["RED"], 0)
        self.assertIsNone(levels["1"].clan)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].user, "?")


class TestBuildPayload(unittest.TestCase):
    def test_ranking_signs_and_recap(self):
        clans = [make_clan("RED", "red", "Red"), make_clan("GREEN", "green", "Green")]
        payload = lb.build_payload(
            clans,
            {"RED": 100, "GREEN": 90},
            {"RED": 5, "GREEN": -2},
            [lb.RatingChange("Level", "alice", "epic", "rated epic", 10)],
            1,
        )
        description = payload["embeds"][0]["description"]
        self.assertIn("1️⃣ **[RED] Red** — **100 pts**  ▲ +5", description)
        self.assertIn("2️⃣ **[GREEN] Green** — **90 pts**  ▼ −2", description)
        self.assertIn("📈 **Rating changes since last check**", description)
        self.assertIn("(+10)", description)
        self.assertIn("━━━━━━━━━━━━━━━━━━━━", description)
        self.assertNotIn("█", description)
        self.assertEqual(payload["embeds"][0]["title"], "🏆 Team leaderboard")
        self.assertEqual(payload["embeds"][0]["color"], 0xFF0000)
        self.assertTrue(payload["embeds"][0]["timestamp"].endswith("Z"))
        self.assertIn("rated levels ·", description)
        self.assertIn(f"<@&{lb.ROLE_ID}> **Leaderboard Update**", payload["content"])
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "roles": [lb.ROLE_ID]})

    def test_no_changes_message(self):
        payload = lb.build_payload([make_clan()], {"RED": 1}, {}, [], 0)
        self.assertIn("✨ _No rating changes since the last check._", payload["embeds"][0]["description"])

    def test_change_list_is_capped(self):
        changes = [
            lb.RatingChange(f"Level {index}", "user", "star", "rated star", 1)
            for index in range(30)
        ]
        payload = lb.build_payload([make_clan()], {}, {}, changes, 1)
        self.assertIn("_…and 15 more_", payload["embeds"][0]["description"])

    def test_description_is_within_discord_limit(self):
        clans = [make_clan(f"C{index}", f"c{index}", "Long " * 20) for index in range(100)]
        payload = lb.build_payload(clans, {}, {}, [], 1)
        description = payload["embeds"][0]["description"]
        self.assertLessEqual(len(description), lb.MAX_DESCRIPTION)
        self.assertTrue(description.endswith("..."))


class TestHttpClient(unittest.TestCase):
    def test_retries_then_succeeds(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=[lb.urllib.error.URLError("a"), lb.urllib.error.URLError("b"), FakeResponse()],
        ), patch("leaderboard.time.sleep") as sleep:
            client = lb.HttpClient(lb.HttpConfig(retries=3, backoff=0.01))
            self.assertEqual(client.get("https://example.test"), "ok")
            self.assertEqual(sleep.call_count, 2)

    def test_exhausted_retries_raise_fetch_error(self):
        with patch("urllib.request.urlopen", side_effect=lb.urllib.error.URLError("down")), patch(
            "leaderboard.time.sleep"
        ):
            client = lb.HttpClient(lb.HttpConfig(retries=2, backoff=0.01))
            with self.assertRaises(lb.FetchError):
                client.get("https://example.test")


class TestParseArgs(unittest.TestCase):
    def test_webhook_required_unless_dry_run_or_reset(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                lb.parse_args([])
            self.assertTrue(lb.parse_args(["--dry-run"]).dry_run)
            self.assertTrue(lb.parse_args(["--reset-baseline"]).reset_baseline)
            self.assertEqual(lb.parse_args(["--webhook", "https://x"]).webhook, "https://x")

    def test_rejects_invalid_tuning_values(self):
        with self.assertRaises(SystemExit):
            lb.parse_args(["--dry-run", "--retries", "0"])
        with self.assertRaises(SystemExit):
            lb.parse_args(["--dry-run", "--max-pages", "0"])
        with self.assertRaises(SystemExit):
            lb.parse_args(["--dry-run", "--timeout", "0"])
        with self.assertRaises(SystemExit):
            lb.parse_args(["--dry-run", "--min-level-ratio", "2"])


class TestMain(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        lb.save_state(self.path, lb.State(points={"RED": 0}, levels={}))
        self.patches = [
            patch.object(lb, "fetch_clans", return_value=[make_clan()]),
            patch.object(lb, "fetch_rosters", return_value={"red": {"Alice"}}),
            patch.object(
                lb,
                "fetch_current_rating",
                return_value=(
                    {"1": lb.Level("1", "100", "star", "First")},
                    {"100": "Alice"},
                ),
            ),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.temp.cleanup)
        for item in reversed(self.patches):
            self.addCleanup(item.stop)

    def test_dry_run_does_not_modify_state(self):
        before = self.path.read_text(encoding="utf-8")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = lb.main(["--dry-run", "--state", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertIn("1️⃣", json.loads(output.getvalue())["embeds"][0]["description"])

    def test_failed_post_is_retried_from_pending_state(self):
        with patch.object(lb.HttpClient, "post_json", side_effect=lb.FetchError("offline")):
            code = lb.main(["--webhook", "https://x", "--state", str(self.path)])
        self.assertEqual(code, 1)
        saved = lb.load_state(self.path)
        self.assertIsNotNone(saved.pending)
        self.assertEqual(saved.points["RED"], 1)

        with patch.object(lb.HttpClient, "post_json", return_value=None) as post:
            code = lb.main(["--webhook", "https://x", "--state", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(post.call_count, 2)
        self.assertIsNone(lb.load_state(self.path).pending)

    def test_empty_api_result_leaves_state_untouched(self):
        before = self.path.read_text(encoding="utf-8")
        lb.fetch_current_rating.return_value = ({}, {})
        code = lb.main(["--webhook", "https://x", "--state", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_api_fetch_error_leaves_state_untouched(self):
        before = self.path.read_text(encoding="utf-8")
        lb.fetch_current_rating.side_effect = lb.FetchError("api down")
        code = lb.main(["--webhook", "https://x", "--state", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_short_api_result_leaves_state_untouched(self):
        lb.save_state(
            self.path,
            lb.State(
                points={"RED": 0},
                levels={str(index): lb.LevelRecord("star", str(index), "A", "RED") for index in range(20)},
            ),
        )
        before = self.path.read_text(encoding="utf-8")
        code = lb.main(["--webhook", "https://x", "--state", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_low_ratio_flag_allows_short_api_result(self):
        lb.save_state(
            self.path,
            lb.State(
                points={"RED": 0},
                levels={str(index): lb.LevelRecord("star", str(index), "A", "RED") for index in range(20)},
            ),
        )
        with patch.object(lb.HttpClient, "post_json", return_value=None):
            code = lb.main(
                ["--webhook", "https://x", "--state", str(self.path), "--min-level-ratio", "0"]
            )
        self.assertEqual(code, 0)


class TestFetchFilter(unittest.TestCase):
    def test_network_error_is_propagated(self):
        client = lb.HttpClient(lb.HttpConfig(retries=1))
        with patch.object(lb, "api", side_effect=lb.FetchError("offline")):
            with self.assertRaises(lb.FetchError):
                lb.fetch_filter(client, {"star": "1"}, 3, 0)

    def test_ignores_single_transient_empty_page(self):
        client = lb.HttpClient(lb.HttpConfig(retries=1))
        pages = [
            ([{"1": "1", "2": "A", "6": "9"}], {}),
            ([], {}),
            ([{"1": "2", "2": "B", "6": "9"}], {}),
            ([], {}),
            ([], {}),
        ]
        with patch.object(lb, "api", side_effect=pages) as api:
            levels, _ = lb.fetch_filter(client, {"star": "1"}, 10, 0)
            self.assertEqual(sorted(levels), ["1", "2"])
            self.assertEqual(api.call_count, 5)

    def test_stops_after_two_empty_pages(self):
        client = lb.HttpClient(lb.HttpConfig(retries=1))
        with patch.object(lb, "api", side_effect=[([{"1": "1", "2": "A", "6": "9"}], {}), ([], {}), ([], {})]) as api:
            levels, _ = lb.fetch_filter(client, {"star": "1"}, 5, 0)
            self.assertEqual(list(levels), ["1"])
            self.assertEqual(api.call_count, 3)


if __name__ == "__main__":
    unittest.main()
