import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock
from urllib import error, parse, request as urllib_request

from flask import Flask, render_template, request

app = Flask(__name__)

LEETIFY_PUBLIC_API = "https://api-public.cs-prod.leetify.com"
REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
}

DEFAULT_PLAYER_CONFIGS = [
    {
        "label": "Przyklad",
        "steam64_id": os.getenv("CS2_SAMPLE_STEAM_ID", "76561198077255766"),
    },
]

ALLOWED_DATA_SOURCES = {
    "matchmaking",
    "matchmaking_competitive",
}
SUMMARY_WINDOWS = (10, 20, 50)
CACHE_TTL_SECONDS = 300
MIN_COMPARE_PLAYERS = 2
MAX_COMPARE_PLAYERS = 5
STEAM64_RE = re.compile(r"^\d{17}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)

_player_cache = {}
_cache_lock = Lock()


def clamp(value, minimum, maximum):
    return max(minimum, min(value, maximum))


def fetch_json(path, **params):
    query = parse.urlencode({key: value for key, value in params.items() if value})
    url = f"{LEETIFY_PUBLIC_API}{path}"
    if query:
        url = f"{url}?{query}"

    req = urllib_request.Request(url, headers=REQUEST_HEADERS)
    with urllib_request.urlopen(req, timeout=20) as response:
        return json.load(response)


def format_map_name(map_name):
    if not map_name:
        return "Unknown"

    clean_name = map_name.replace("de_", "").replace("cs_", "").replace("_", " ")
    return " ".join(part.capitalize() if not part.isupper() else part for part in clean_name.split())


def format_data_source(data_source):
    labels = {
        "matchmaking": "Premier",
        "matchmaking_competitive": "Competitive 5v5",
        "matchmaking_wingman": "Wingman",
        "faceit": "FACEIT",
        "renown": "Renown",
    }
    return labels.get(data_source, data_source.replace("_", " ").title())


def safe_round(value, digits=2):
    if value is None:
        return None
    return round(value, digits)


def average(values, digits=2):
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return round(sum(cleaned) / len(cleaned), digits)


def format_metric(value, suffix="", digits=1):
    if value is None:
        return "-"
    if isinstance(value, (int, float)) and math.isinf(value):
        return "Perfect"
    if isinstance(value, (int, float)) and digits == 0:
        return f"{int(round(value))}{suffix}"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def format_timestamp(value):
    if not value:
        return "-"

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def compute_outcome(team_scores, team_number):
    my_score = None
    enemy_score = None

    for team_score in team_scores or []:
        if team_score.get("team_number") == team_number:
            my_score = team_score.get("score", 0)
        else:
            enemy_score = team_score.get("score", 0)

    if my_score is None or enemy_score is None:
        return "unknown", "-"
    if my_score > enemy_score:
        return "win", f"{my_score}:{enemy_score}"
    if my_score < enemy_score:
        return "loss", f"{my_score}:{enemy_score}"
    return "tie", f"{my_score}:{enemy_score}"


def normalize_match(match, player_steam64_id):
    player_stats = next(
        (stats for stats in match.get("stats", []) if stats.get("steam64_id") == player_steam64_id),
        (match.get("stats") or [{}])[0],
    )

    rounds_count = player_stats.get("rounds_count") or 0
    total_damage = player_stats.get("total_damage") or 0
    total_kills = player_stats.get("total_kills") or 0
    total_hs_kills = player_stats.get("total_hs_kills") or 0

    outcome, scoreline = compute_outcome(
        match.get("team_scores"),
        player_stats.get("initial_team_number"),
    )

    adr = None
    if rounds_count:
        adr = round(total_damage / rounds_count, 1)

    hs_percentage = None
    if total_kills:
        hs_percentage = round((total_hs_kills / total_kills) * 100, 1)

    return {
        "id": match.get("id"),
        "finished_at": match.get("finished_at"),
        "finished_at_display": format_timestamp(match.get("finished_at")),
        "data_source": match.get("data_source"),
        "data_source_label": format_data_source(match.get("data_source", "")),
        "map_name": match.get("map_name"),
        "map_label": format_map_name(match.get("map_name")),
        "outcome": outcome,
        "scoreline": scoreline,
        "kills": total_kills,
        "deaths": player_stats.get("total_deaths") or 0,
        "assists": player_stats.get("total_assists") or 0,
        "adr": adr,
        "hs_percentage": hs_percentage,
        "leetify_rating": safe_round(player_stats.get("leetify_rating"), 3),
        "preaim": safe_round(player_stats.get("preaim"), 1),
        "reaction_time_ms": safe_round((player_stats.get("reaction_time") or 0) * 1000, 0),
        "mvps": player_stats.get("mvps") or 0,
        "rounds_count": rounds_count,
    }


def summarize_matches(matches, limit):
    selected_matches = matches[:limit]
    played = len(selected_matches)
    if not played:
        return {
            "played": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "winrate": None,
            "average_kills": None,
            "average_deaths": None,
            "average_assists": None,
            "average_kd": None,
            "average_adr": None,
            "average_hs": None,
            "average_rating": None,
            "average_preaim": None,
            "average_reaction_time_ms": None,
        }

    wins = sum(1 for match in selected_matches if match["outcome"] == "win")
    losses = sum(1 for match in selected_matches if match["outcome"] == "loss")
    ties = sum(1 for match in selected_matches if match["outcome"] == "tie")

    total_kills = sum(match["kills"] for match in selected_matches)
    total_deaths = sum(match["deaths"] for match in selected_matches)

    average_kd = None
    if total_deaths:
        average_kd = round(total_kills / total_deaths, 2)
    elif total_kills:
        average_kd = math.inf

    return {
        "played": played,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "winrate": round((wins / played) * 100, 1),
        "average_kills": average([match["kills"] for match in selected_matches], 1),
        "average_deaths": average([match["deaths"] for match in selected_matches], 1),
        "average_assists": average([match["assists"] for match in selected_matches], 1),
        "average_kd": average_kd,
        "average_adr": average([match["adr"] for match in selected_matches], 1),
        "average_hs": average([match["hs_percentage"] for match in selected_matches], 1),
        "average_rating": average([match["leetify_rating"] for match in selected_matches], 3),
        "average_preaim": average([match["preaim"] for match in selected_matches], 1),
        "average_reaction_time_ms": average(
            [match["reaction_time_ms"] for match in selected_matches],
            0,
        ),
    }


def build_summary_view(summary):
    return {
        "played": summary["played"],
        "record": f"{summary['wins']}-{summary['losses']}-{summary['ties']}",
        "winrate": format_metric(summary["winrate"], "%", 1),
        "kda": (
            f"{format_metric(summary['average_kills'], '', 1)} / "
            f"{format_metric(summary['average_deaths'], '', 1)} / "
            f"{format_metric(summary['average_assists'], '', 1)}"
        ),
        "kd": format_metric(summary["average_kd"], "", 2),
        "adr": format_metric(summary["average_adr"], "", 1),
        "hs": format_metric(summary["average_hs"], "%", 1),
        "rating": format_metric(summary["average_rating"], "", 3),
        "preaim": format_metric(summary["average_preaim"], "", 1),
        "reaction": format_metric(summary["average_reaction_time_ms"], " ms", 0),
    }


def comparison_metrics():
    return [
        {"key": "record", "label": "Bilans W-L-T"},
        {"key": "winrate", "label": "Winrate"},
        {"key": "kda", "label": "AVG K / D / A"},
        {"key": "kd", "label": "AVG K/D"},
        {"key": "adr", "label": "AVG ADR"},
        {"key": "hs", "label": "AVG HS%"},
        {"key": "rating", "label": "AVG Rating"},
        {"key": "preaim", "label": "AVG Preaim"},
        {"key": "reaction", "label": "AVG Reaction"},
    ]


def build_comparison_rows(players):
    rows_by_window = {}
    metrics = comparison_metrics()

    for window in SUMMARY_WINDOWS:
        rows = []
        for metric in metrics:
            rows.append(
                {
                    "label": metric["label"],
                    "values": [
                        (
                            player["summaries"][window][metric["key"]]
                            if not player.get("error")
                            else "Error"
                        )
                        for player in players
                    ],
                }
            )
        rows_by_window[window] = rows

    return rows_by_window


def get_cached_player(key):
    with _cache_lock:
        cached_item = _player_cache.get(key)
        if not cached_item:
            return None

        if time.time() - cached_item["stored_at"] > CACHE_TTL_SECONDS:
            _player_cache.pop(key, None)
            return None

        return cached_item["payload"]


def set_cached_player(key, payload):
    with _cache_lock:
        _player_cache[key] = {
            "stored_at": time.time(),
            "payload": payload,
        }


def parse_player_reference(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return None

    parsed_url = parse.urlparse(value)
    if parsed_url.scheme and parsed_url.netloc:
        path_parts = [part for part in parsed_url.path.split("/") if part]
        if path_parts:
            value = path_parts[-1]

    if STEAM64_RE.match(value):
        return {
            "steam64_id": value,
            "label": value,
            "input_value": raw_value,
        }

    if UUID_RE.match(value):
        return {
            "id": value,
            "label": value,
            "input_value": raw_value,
        }

    return {
        "label": raw_value,
        "input_value": raw_value,
        "error": (
            "Wpisz Steam64 ID, Leetify user ID albo link do profilu Leetify. "
            "Wyszukiwanie po samym nicku Steam nie jest dostepne w publicznym API."
        ),
    }


def get_default_player_inputs():
    values = []
    for player in DEFAULT_PLAYER_CONFIGS:
        values.append(player.get("steam64_id") or player.get("id") or "")
    return values[:MAX_COMPARE_PLAYERS]


def build_input_slots(raw_inputs, slots_count):
    slots = []
    padded_inputs = raw_inputs[:MAX_COMPARE_PLAYERS] + [""] * MAX_COMPARE_PLAYERS

    for index in range(MAX_COMPARE_PLAYERS):
        slots.append(
            {
                "index": index + 1,
                "value": padded_inputs[index],
                "visible": index < slots_count,
            }
        )
    return slots


def load_player_card(player_config):
    cache_key = player_config.get("steam64_id") or player_config.get("id")
    cached_payload = get_cached_player(cache_key) if cache_key else None
    if cached_payload:
        return cached_payload

    if player_config.get("error"):
        return {
            "label": player_config.get("label") or player_config.get("input_value") or "-",
            "name": player_config.get("label") or player_config.get("input_value") or "-",
            "steam64_id": player_config.get("steam64_id") or "-",
            "input_value": player_config.get("input_value") or "",
            "error": player_config["error"],
        }

    try:
        if player_config.get("steam64_id"):
            profile = fetch_json("/v3/profile", steam64_id=player_config["steam64_id"])
            matches = fetch_json("/v3/profile/matches", steam64_id=player_config["steam64_id"])
        else:
            profile = fetch_json("/v3/profile", id=player_config.get("id"))
            matches = fetch_json("/v3/profile/matches", id=player_config.get("id"))
    except error.HTTPError as exc:
        if exc.code == 404:
            message = (
                "Nie znaleziono gracza w publicznym API Leetify. "
                "Najczesciej oznacza to zly Steam64, brak profilu w Leetify "
                "albo niepubliczny profil."
            )
        else:
            message = f"API error {exc.code}"
        payload = {
            "label": player_config.get("label") or player_config.get("input_value") or "-",
            "name": player_config.get("label") or player_config.get("input_value") or "-",
            "steam64_id": player_config.get("steam64_id") or "-",
            "input_value": player_config.get("input_value") or "",
            "error": message,
        }
        if cache_key:
            set_cached_player(cache_key, payload)
        return payload
    except Exception as exc:  # pragma: no cover
        payload = {
            "label": player_config.get("label") or player_config.get("input_value") or "-",
            "name": player_config.get("label") or player_config.get("input_value") or "-",
            "steam64_id": player_config.get("steam64_id") or "-",
            "input_value": player_config.get("input_value") or "",
            "error": str(exc),
        }
        if cache_key:
            set_cached_player(cache_key, payload)
        return payload

    filtered_matches = [
        normalize_match(match, profile.get("steam64_id"))
        for match in matches
        if match.get("data_source") in ALLOWED_DATA_SOURCES
    ]

    summaries = {
        window: build_summary_view(summarize_matches(filtered_matches, window))
        for window in SUMMARY_WINDOWS
    }

    payload = {
        "label": profile.get("name") or player_config.get("label") or cache_key,
        "name": profile.get("name") or player_config.get("label") or cache_key,
        "steam64_id": profile.get("steam64_id") or player_config.get("steam64_id") or "-",
        "input_value": player_config.get("input_value") or cache_key or "",
        "privacy_mode": profile.get("privacy_mode", "unknown"),
        "premier_rank": profile.get("ranks", {}).get("premier"),
        "leetify_rank": safe_round(profile.get("ranks", {}).get("leetify"), 2),
        "total_matches": profile.get("total_matches", 0),
        "first_match_date": format_timestamp(profile.get("first_match_date")),
        "profile_winrate": format_metric(
            None if profile.get("winrate") is None else profile.get("winrate") * 100,
            "%",
            1,
        ),
        "summaries": summaries,
        "matches_count": len(filtered_matches),
        "latest_matches": filtered_matches[:5],
        "error": None,
    }
    if cache_key:
        set_cached_player(cache_key, payload)
    return payload


def load_dashboard(player_configs):
    if not player_configs:
        return []

    max_workers = min(8, len(player_configs)) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(load_player_card, player_config): index
            for index, player_config in enumerate(player_configs)
        }
        ordered_cards = [None] * len(futures)

        for future in as_completed(futures):
            ordered_cards[futures[future]] = future.result()

    return [card for card in ordered_cards if card is not None]


def resolve_requested_players(raw_inputs):
    configs = []
    for raw_value in raw_inputs[:MAX_COMPARE_PLAYERS]:
        if not raw_value.strip():
            continue
        parsed_player = parse_player_reference(raw_value)
        if parsed_player:
            configs.append(parsed_player)

    if configs:
        return configs

    fallback_configs = []
    for player in DEFAULT_PLAYER_CONFIGS[:MAX_COMPARE_PLAYERS]:
        value = player.get("steam64_id") or player.get("id")
        parsed_player = parse_player_reference(value)
        if parsed_player:
            parsed_player["label"] = player.get("label") or parsed_player.get("label")
            fallback_configs.append(parsed_player)
    return fallback_configs


@app.route("/")
def index():
    raw_inputs = request.args.getlist("player")
    if not raw_inputs:
        raw_inputs = get_default_player_inputs()

    filled_count = len([value for value in raw_inputs if value.strip()])
    requested_slots = request.args.get("slots", type=int)
    if requested_slots is None:
        requested_slots = max(MIN_COMPARE_PLAYERS, min(MAX_COMPARE_PLAYERS, filled_count or len(DEFAULT_PLAYER_CONFIGS)))
    slots_count = clamp(requested_slots, MIN_COMPARE_PLAYERS, MAX_COMPARE_PLAYERS)

    player_configs = resolve_requested_players(raw_inputs)
    cards = load_dashboard(player_configs)
    valid_cards = [card for card in cards if not card.get("error")]
    comparison_players = valid_cards if len(valid_cards) >= 1 else []

    return render_template(
        "index.html",
        players=cards,
        valid_players_count=len(valid_cards),
        comparison_players=comparison_players,
        comparison_rows=build_comparison_rows(comparison_players) if comparison_players else {},
        windows=SUMMARY_WINDOWS,
        default_window=SUMMARY_WINDOWS[0],
        generated_at=format_timestamp(datetime.now(timezone.utc).isoformat()),
        slots_count=slots_count,
        input_slots=build_input_slots(raw_inputs, slots_count),
        min_compare_players=MIN_COMPARE_PLAYERS,
        max_compare_players=MAX_COMPARE_PLAYERS,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
