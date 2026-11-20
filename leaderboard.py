#!/usr/bin/env python3
"""SplitGDPS Clan Rate-Points Leaderboard -> Discord webhook.

Tracks cumulative points per clan from a seeded baseline. On each run the full
rated-level set is recomputed: new ratings add points, upgrades add the
difference, and demotions / un-ratings subtract it.

Scoring per rated level by its highest tier:
  star       =  1 pt
  featured   =  5 pts
  epic       = 10 pts
  legendary  = 20 pts
  mythic     = 40 pts

State is stored in state.json with an atomic write. A payload is persisted as
"pending" before delivery and cleared only after a successful webhook post, so
a failed delivery is retried on the next run instead of losing the changes.

Usage:
  python leaderboard.py                    # update + post to Discord
  python leaderboard.py --dry-run          # print the payload, change nothing
  python leaderboard.py --reset-baseline   # reseed state from BASELINE
  python leaderboard.py --state FILE       # use a custom state path
  python leaderboard.py --retries 5        # tune network resilience
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

LOG = logging.getLogger("leaderboard")

DASH = "https://splitgdps.alwaysdata.net/dashboard"
INDEX_URL = DASH + "/clans/"
API_URL = "https://splitgdps.alwaysdata.net/getGJLevels21.php"
USER_AGENT = "SplitGDPS-Clans-Leaderboard-Bot/6.0"
ROLE_ID = "1485811491667185816"

TIER_POINTS: dict[str, int] = {
    "mythic": 40,
    "legendary": 20,
    "epic": 10,
    "featured": 5,
    "star": 1,
}
TIER_ORDER = ("mythic", "legendary", "epic", "featured", "star")
TIER_ICON = {"mythic": "🌟", "legendary": "👑", "epic": "💎", "featured": "❕", "star": "⭐"}

MEDALS = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣"}
BASELINE = {"RED": 615, "GREEN": 555, "BLUE": 323, "YELLO": 158}

STATE_VERSION = 2
MAX_CHANGES = 15
MAX_DESCRIPTION = 4096
EMPTY_PAGE_LIMIT = 2
DEFAULT_MIN_LEVEL_RATIO = 0.8

API_BASE_PARAMS = {
    "secret": "Wmfd2893gb7",
    "type": "0",
    "page": "0",
    "total": "0",
    "gameVersion": "22",
    "binaryVersion": "45",
}

ROSTER_PATTERNS = (
    r"profilenick clanmembernick[^>]*>.*?<img[^>]*>([^<]+)",
    r"clanownernick[^>]*>.*?<img[^>]*>([^<]+)",
)


class FetchError(RuntimeError):
    """Raised when an HTTP request still fails after all retries."""


class StateError(ValueError):
    """Raised when a state file has an unexpected structure."""


@dataclass(frozen=True)
class Clan:
    name: str
    tag: str
    color: int
    members: int
    slug: str

    @property
    def key(self) -> str:
        return self.tag or self.slug.upper()


@dataclass(frozen=True)
class Level:
    level_id: str
    uid: str
    tier: str
    name: str


@dataclass(frozen=True)
class LevelRecord:
    tier: str
    uid: str
    user: str
    clan: str | None = None
    credit: int = 0


@dataclass(frozen=True)
class RatingChange:
    name: str
    user: str
    tier: str
    label: str
    points: int


@dataclass(frozen=True)
class PendingPost:
    payload: dict[str, Any]
    created_at: str


@dataclass
class State:
    points: dict[str, int]
    levels: dict[str, LevelRecord]
    pending: PendingPost | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": STATE_VERSION,
            "points": dict(self.points),
            "levels": {
                lid: {
                    "tier": record.tier,
                    "uid": record.uid,
                    "user": record.user,
                    "clan": record.clan,
                    "credit": record.credit,
                }
                for lid, record in self.levels.items()
            },
        }
        if self.pending is not None:
            data["pending"] = {
                "created_at": self.pending.created_at,
                "payload": self.pending.payload,
            }
        return data

    @classmethod
    def from_json(cls, raw: Any) -> State:
        if not isinstance(raw, dict):
            raise StateError("state root must be an object")

        raw_points = raw.get("points", {})
        if not isinstance(raw_points, dict):
            raise StateError("'points' must be an object")
        points: dict[str, int] = {}
        for tag, value in raw_points.items():
            try:
                points[str(tag)] = int(value)
            except (TypeError, ValueError):
                LOG.warning("Ignoring invalid point value for clan %r", tag)

        raw_levels = raw.get("levels", {})
        if not isinstance(raw_levels, dict):
            raise StateError("'levels' must be an object")
        levels: dict[str, LevelRecord] = {}
        for lid, item in raw_levels.items():
            if not isinstance(item, dict):
                LOG.warning("Ignoring malformed level record %r", lid)
                continue
            tier = str(item.get("tier", ""))
            if tier not in TIER_POINTS:
                LOG.warning("Ignoring level %r with unknown tier %r", lid, tier)
                continue
            clan_value = item.get("clan")
            clan = str(clan_value) if clan_value else None
            credit_value = item.get("credit")
            try:
                credit = int(credit_value)
            except (TypeError, ValueError):
                credit = TIER_POINTS[tier] if clan else 0
            levels[str(lid)] = LevelRecord(
                tier=tier,
                uid=str(item.get("uid", "?")),
                user=str(item.get("user", "")),
                clan=clan,
                credit=credit,
            )

        pending: PendingPost | None = None
        raw_pending = raw.get("pending")
        if isinstance(raw_pending, dict) and isinstance(raw_pending.get("payload"), dict):
            pending = PendingPost(
                payload=raw_pending["payload"],
                created_at=str(raw_pending.get("created_at", "")),
            )
        return cls(points=points, levels=levels, pending=pending)


@dataclass(frozen=True)
class HttpConfig:
    timeout: float = 30.0
    retries: int = 3
    backoff: float = 1.0
    page_delay: float = 0.0
    max_pages: int = 200


class HttpClient:
    """Small urllib wrapper with bounded retries and exponential backoff."""

    def __init__(self, config: HttpConfig) -> None:
        self.config = config

    def request(self, req: urllib.request.Request) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout) as response:
                    return response.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt < self.config.retries:
                    delay = self.config.backoff * (2 ** (attempt - 1))
                    LOG.warning(
                        "Request to %s failed (%s); retry %d/%d in %.1fs",
                        req.full_url,
                        exc,
                        attempt,
                        self.config.retries,
                        delay,
                    )
                    time.sleep(delay)
        raise FetchError(
            f"request to {req.full_url} failed after {self.config.retries} attempts: {last_error}"
        ) from last_error

    def get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return self.request(req)

    def post_json(self, url: str, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        self.request(req)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_display_time(stamp: str) -> str:
    try:
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return stamp
    return parsed.strftime("%d %b %Y %H:%M UTC")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return cut + "..."


def clean_username(name: str) -> str:
    return html_lib.unescape(re.sub(r"^\[[^\]]+\]\s*", "", name.strip())).strip()


def parse_levels(resp: str) -> tuple[list[dict[str, str]], dict[str, str]]:
    if "#" not in resp:
        return [], {}
    main, rest = resp.split("#", 1)
    user_part = rest.split("#", 1)[0]

    users: dict[str, str] = {}
    for entry in user_part.split("|"):
        fields = entry.split(":")
        if len(fields) >= 2:
            users[fields[0]] = fields[1]

    levels: list[dict[str, str]] = []
    for chunk in main.split("|"):
        tokens = chunk.split(":")
        if len(tokens) < 2:
            continue
        levels.append({tokens[i]: tokens[i + 1] for i in range(0, len(tokens) - 1, 2)})
    return levels, users


def parse_clans(html_text: str) -> list[Clan]:
    clans: list[Clan] = []
    for chunk in html_text.split('<div class="profile clanscard">')[1:]:
        header = re.search(r"color:#([0-9a-fA-F]{6})[^>]*>(.*?)</span>", chunk, re.S)
        if not header:
            continue
        slug = re.search(r"clan/([A-Za-z0-9_]+)", chunk)
        if not slug:
            continue
        content = html_lib.unescape(re.sub(r"<[^>]*>", "", header.group(2))).strip()
        tagged = re.match(r"\[([A-Za-z0-9]+)\]\s*(.+)", content)
        tag = tagged.group(1) if tagged else ""
        name = tagged.group(2).strip() if tagged else content
        if not name:
            continue
        members = re.search(r"<b>(\d+)</b>\s*members?", chunk)
        clans.append(
            Clan(
                name=f"[{tag}] {name}" if tag else name,
                tag=tag,
                color=int(header.group(1), 16),
                members=int(members.group(1)) if members else 0,
                slug=slug.group(1),
            )
        )
    return clans


def parse_roster(html_text: str) -> set[str]:
    names = {
        html_lib.unescape(match.group(1)).strip()
        for pattern in ROSTER_PATTERNS
        for match in re.finditer(pattern, html_text, re.S)
    }
    return {name for name in names if name}


def build_user_clan_map(clans: Sequence[Clan], rosters: Mapping[str, set[str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for clan in clans:
        for username in sorted(rosters.get(clan.slug, ())):
            key = clean_username(username).casefold()
            if key and key not in mapping:
                mapping[key] = clan.key
    return mapping


def api(
    http: HttpClient, filters: Mapping[str, str], page: int
) -> tuple[list[dict[str, str]], dict[str, str]]:
    params: dict[str, str] = dict(API_BASE_PARAMS)
    params.update(filters)
    params["page"] = str(page)
    body = urllib.parse.urlencode(params).encode("ascii")
    req = urllib.request.Request(API_URL, data=body, headers={"User-Agent": USER_AGENT})
    return parse_levels(http.request(req))


def fetch_filter(
    http: HttpClient, filters: Mapping[str, str], max_pages: int, page_delay: float
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    raw_levels: dict[str, dict[str, str]] = {}
    users: dict[str, str] = {}
    empty_streak = 0
    for page in range(max_pages):
        try:
            page_levels, page_users = api(http, filters, page)
        except FetchError as exc:
            LOG.error("Aborting %s after page %d: %s", dict(filters), page, exc)
            raise
        users.update(page_users)
        if not page_levels:
            empty_streak += 1
            if empty_streak >= EMPTY_PAGE_LIMIT:
                break
        else:
            empty_streak = 0
            for level in page_levels:
                level_id = level.get("1")
                if level_id and level_id not in raw_levels:
                    raw_levels[level_id] = level
        if page_delay:
            time.sleep(page_delay)
    return raw_levels, users


def fetch_current_rating(
    http: HttpClient, max_pages: int, page_delay: float
) -> tuple[dict[str, Level], dict[str, str]]:
    """Return the highest tier for each rated level plus the API user map.

    The GD protocol swaps the legendary/mythic search parameters, so the
    filter named "mythic" uses `legendary=1` and vice versa.
    """
    tiers: dict[str, Level] = {}
    users: dict[str, str] = {}
    filters_by_tier = (
        ("mythic", {"legendary": "1"}),
        ("legendary", {"mythic": "1"}),
        ("epic", {"epic": "1"}),
        ("featured", {"featured": "1"}),
        ("star", {"star": "1"}),
    )
    for tier, filters in filters_by_tier:
        levels, page_users = fetch_filter(http, filters, max_pages, page_delay)
        users.update(page_users)
        for level_id, raw in levels.items():
            candidate = Level(
                level_id=level_id,
                uid=raw.get("6", "?"),
                tier=tier,
                name=raw.get("2", "?"),
            )
            existing = tiers.get(level_id)
            if existing is None or TIER_POINTS[candidate.tier] > TIER_POINTS[existing.tier]:
                tiers[level_id] = candidate
    return tiers, users


def fetch_clans(http: HttpClient) -> list[Clan]:
    return parse_clans(http.get(INDEX_URL))


def fetch_rosters(
    http: HttpClient, clans: Sequence[Clan], page_delay: float
) -> dict[str, set[str]]:
    rosters: dict[str, set[str]] = {}
    for clan in clans:
        try:
            rosters[clan.slug] = parse_roster(http.get(f"{DASH}/clan/{clan.slug}"))
        except FetchError as exc:
            LOG.warning("Could not fetch roster for %s: %s", clan.name, exc)
            rosters[clan.slug] = set()
        if page_delay:
            time.sleep(page_delay)
    return rosters


def load_state(path: Path) -> State:
    if not path.exists():
        return State(points=dict(BASELINE), levels={})
    try:
        with path.open(encoding="utf-8") as handle:
            return State.from_json(json.load(handle))
    except (OSError, ValueError, StateError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
        try:
            shutil.copy2(path, backup)
        except OSError as copy_error:
            LOG.warning("Could not back up the unreadable state file: %s", copy_error)
        LOG.error(
            "State file %s is unreadable (%s); backed up to %s and starting from baseline",
            path,
            exc,
            backup,
        )
        return State(points=dict(BASELINE), levels={})


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state.to_json(), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def compute_update(
    tiers: Mapping[str, Level],
    users: Mapping[str, str],
    user_clans: Mapping[str, str],
    clan_tags: Sequence[str],
    state: State,
) -> tuple[dict[str, int], dict[str, LevelRecord], dict[str, int], list[RatingChange]]:
    points = dict(state.points)
    levels = dict(state.levels)
    net = {tag: 0 for tag in dict.fromkeys(clan_tags)}
    changes: list[RatingChange] = []

    for level_id, level in tiers.items():
        old = levels.get(level_id)
        old_tier = old.tier if old else None
        user = clean_username(users.get(level.uid, "?"))
        clan = user_clans.get(user.casefold()) or None

        if old is not None and old.tier == level.tier and old.uid == level.uid:
            continue

        diff = TIER_POINTS[level.tier] - TIER_POINTS.get(old_tier, 0)

        if old is not None and old.uid != level.uid and old_tier == level.tier:
            diff = 0

        credited = diff if clan else 0
        if credited:
            points[clan] = points.get(clan, 0) + credited
            net[clan] = net.get(clan, 0) + credited

        if diff:
            label = f"rated {level.tier}" if old_tier is None else f"{old_tier} → {level.tier}"
            shown_tier = (
                level.tier
                if old_tier is None
                else max(level.tier, old_tier, key=lambda tier: TIER_POINTS[tier])
            )
            changes.append(
                RatingChange(
                    name=level.name,
                    user=user,
                    tier=shown_tier,
                    label=label,
                    points=diff,
                )
            )

        if old is None:
            credit = credited
        elif old.clan == clan:
            credit = old.credit + credited
        else:
            credit = credited
        levels[level_id] = LevelRecord(
            tier=level.tier, uid=level.uid, user=user, clan=clan, credit=credit
        )

    for level_id, record in list(levels.items()):
        if level_id in tiers:
            continue
        removed_points = record.credit
        if removed_points and record.clan:
            points[record.clan] = points.get(record.clan, 0) - removed_points
            net[record.clan] = net.get(record.clan, 0) - removed_points
            changes.append(
                RatingChange(
                    name="(unrated level)",
                    user=record.user,
                    tier=record.tier,
                    label=f"{record.tier} → unrated",
                    points=-removed_points,
                )
            )
        del levels[level_id]

    return points, levels, net, changes


def build_payload(
    clans: Sequence[Clan],
    points: Mapping[str, int],
    net: Mapping[str, int],
    changes: Sequence[RatingChange],
    total_levels: int,
) -> dict[str, Any]:
    ranked = sorted(
        ((clan, points.get(clan.key, 0)) for clan in clans),
        key=lambda item: (-item[1], item[0].name.casefold()),
    )
    stamp = utc_now()
    divider = "━━━━━━━━━━━━━━━━━━━━"
    lines = [f"*{total_levels:,} rated levels · {format_display_time(stamp)}*", divider, ""]

    for rank, (clan, points_value) in enumerate(ranked, start=1):
        delta = net.get(clan.key, 0)
        if delta > 0:
            movement = f"  ▲ +{delta:,}"
        elif delta < 0:
            movement = f"  ▼ −{abs(delta):,}"
        else:
            movement = ""
        unit = "pt" if points_value == 1 else "pts"
        lines.append(f"{MEDALS.get(rank, '⬜')} **{clan.name}** — **{points_value:,} {unit}**{movement}")

    lines.extend(["", divider, ""])
    if changes:
        lines.append("📈 **Rating changes since last check**")
        shown = sorted(changes, key=lambda change: (-change.points, change.user.casefold()))[
            :MAX_CHANGES
        ]
        for change in shown:
            lines.append(
                f"   {TIER_ICON[change.tier]} **{change.name}** by *{change.user}* "
                f"— {change.label} ({change.points:+d})"
            )
        if len(changes) > MAX_CHANGES:
            lines.append(f"   _…and {len(changes) - MAX_CHANGES} more_")
    else:
        lines.append("✨ _No rating changes since the last check._")

    embed = {
        "title": "🏆 Team leaderboard",
        "description": truncate("\n".join(lines), MAX_DESCRIPTION),
        "color": ranked[0][0].color if ranked else 0xF1C40F,
        "timestamp": stamp,
        "footer": {"text": "⭐ 1 · ❕ 5 · 💎 10 · 👑 20 · 🌟 40 points per rated level"},
    }
    return {
        "content": f"<@&{ROLE_ID}> **Leaderboard Update**",
        "allowed_mentions": {"parse": [], "roles": [ROLE_ID]},
        "embeds": [embed],
    }


def emit_json(payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            raise
        buffer.write(text.encode("utf-8", "replace") + b"\n")
    stream.flush()


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update the SplitGDPS clan rate-points leaderboard and post it to Discord.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--webhook",
        default=os.environ.get("WEBHOOK_URL"),
        help="Discord webhook URL (default: WEBHOOK_URL environment variable)",
    )
    parser.add_argument("--state", type=Path, default=Path("state.json"), help="state file path")
    parser.add_argument("--dry-run", action="store_true", help="print the payload without posting or saving")
    parser.add_argument("--reset-baseline", action="store_true", help="reseed state from the manual baseline")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="attempts per HTTP request")
    parser.add_argument("--backoff", type=float, default=1.0, help="initial retry backoff in seconds")
    parser.add_argument("--page-delay", type=float, default=0.0, help="delay between API pages in seconds")
    parser.add_argument("--max-pages", type=int, default=200, help="maximum pages per rating filter")
    parser.add_argument(
        "--min-level-ratio",
        type=float,
        default=DEFAULT_MIN_LEVEL_RATIO,
        help="abort if the API returns fewer than this fraction of tracked levels",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args(argv)

    if not args.webhook and not args.dry_run and not args.reset_baseline:
        parser.error("provide --webhook URL or set WEBHOOK_URL (or use --dry-run/--reset-baseline)")
    if args.retries < 1:
        parser.error("--retries must be at least 1")
    if args.max_pages < 1:
        parser.error("--max-pages must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.backoff < 0 or args.page_delay < 0:
        parser.error("--backoff and --page-delay cannot be negative")
    if not 0 <= args.min_level_ratio <= 1:
        parser.error("--min-level-ratio must be between 0 and 1")
    return args


def http_config_from_args(args: argparse.Namespace) -> HttpConfig:
    return HttpConfig(
        timeout=args.timeout,
        retries=args.retries,
        backoff=args.backoff,
        page_delay=args.page_delay,
        max_pages=args.max_pages,
    )


def reset_baseline(http: HttpClient, path: Path, max_pages: int, page_delay: float) -> int:
    clans = fetch_clans(http)
    rosters = fetch_rosters(http, clans, page_delay) if clans else {}
    user_clans = build_user_clan_map(clans, rosters)
    tiers, users = fetch_current_rating(http, max_pages, page_delay)
    if not tiers:
        LOG.error("The API returned no rated levels; baseline not changed")
        return 1

    levels: dict[str, LevelRecord] = {}
    for level_id, level in tiers.items():
        user = clean_username(users.get(level.uid, "?"))
        clan = user_clans.get(user.casefold())
        levels[level_id] = LevelRecord(
            tier=level.tier,
            uid=level.uid,
            user=user,
            clan=clan,
            credit=TIER_POINTS[level.tier] if clan else 0,
        )
    state = State(points=dict(BASELINE), levels=levels, pending=None)
    save_state(path, state)
    LOG.info("Baseline reseeded to %s (%d levels recorded)", state.points, len(levels))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    http = HttpClient(http_config_from_args(args))
    state_path = args.state

    if args.reset_baseline:
        try:
            return reset_baseline(http, state_path, args.max_pages, args.page_delay)
        except FetchError as exc:
            LOG.error("Could not reset baseline: %s", exc)
            return 1

    state = load_state(state_path)
    if state.pending is not None:
        if args.dry_run:
            LOG.info("A pending payload exists; dry-run leaves it untouched")
        else:
            try:
                http.post_json(args.webhook, state.pending.payload)
            except FetchError as exc:
                LOG.error("Pending webhook delivery failed: %s", exc)
                return 1
            created_at = state.pending.created_at
            state.pending = None
            save_state(state_path, state)
            LOG.info("Delivered pending payload created at %s", created_at or "an earlier run")

    try:
        clans = fetch_clans(http)
    except FetchError as exc:
        LOG.error("Could not fetch clans: %s; state left untouched", exc)
        return 1
    LOG.info("Found %d clans on the dashboard", len(clans))
    if not clans:
        LOG.error("No clans found on the dashboard; state left untouched")
        return 1

    rosters = fetch_rosters(http, clans, args.page_delay)
    try:
        tiers, users = fetch_current_rating(http, args.max_pages, args.page_delay)
    except FetchError as exc:
        LOG.error("Could not fetch rated levels: %s; state left untouched", exc)
        return 1
    LOG.info("Found %d rated levels", len(tiers))
    if not tiers:
        LOG.error("The API returned no rated levels; state left untouched")
        return 1
    if state.levels:
        minimum = max(1, int(len(state.levels) * args.min_level_ratio))
        if len(tiers) < minimum:
            LOG.error(
                "Only %d rated levels found but %d are tracked (expected at least %d); "
                "state left untouched",
                len(tiers),
                len(state.levels),
                minimum,
            )
            return 1

    user_clans = build_user_clan_map(clans, rosters)
    points, levels, net, changes = compute_update(
        tiers,
        users,
        user_clans,
        [clan.key for clan in clans],
        state,
    )
    payload = build_payload(clans, points, net, changes, len(tiers))

    if args.dry_run:
        emit_json(payload)
        LOG.info("Dry run complete: %d tracked levels, %d changes", len(levels), len(changes))
        return 0

    state.points = points
    state.levels = levels
    state.pending = PendingPost(payload=payload, created_at=utc_now())
    save_state(state_path, state)

    try:
        http.post_json(args.webhook, payload)
    except FetchError as exc:
        LOG.error(
            "Webhook delivery failed (%s); payload saved as pending for the next run",
            exc,
        )
        return 1

    state.pending = None
    save_state(state_path, state)
    LOG.info("Posted leaderboard update to Discord (%d changes)", len(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
