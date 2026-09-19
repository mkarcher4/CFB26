#!/usr/bin/env python3
"""
Fetches ESPN's public CFB scoreboard for every week of the season and writes
data/schedule.json in the shape the app expects:

    { "<Person>": [ [ {opp, fcs, status, score}, ... 17 weeks ... ], ... 14 teams ... ] }

Run by .github/workflows/sync.yml on a schedule. Safe to run locally too:
    pip install requests
    python3 scripts/fetch_espn.py

This intentionally mirrors the matching logic that used to run client-side in
the browser (normalizeTeamName / NAME_ALIASES / ESPN_WEEK_PARAMS) -- keep the
two in sync if you add a team or an alias in one place, add it in the other.
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRAFT_PATH = os.path.join(ROOT, "data", "draft.json")
SCHEDULE_PATH = os.path.join(ROOT, "data", "schedule.json")

WEEKS = ["Week 0","Week 1","Week 2","Week 3","Week 4","Week 5","Week 6","Week 7","Week 8",
         "Week 9","Week 10","Week 11","Week 12","Week 13","Week 14","Week 15","Postseason"]

# Week 0 and Week 1 use explicit date ranges because ESPN's own "week=1" bucket merges the
# Aug 29 openers and the true Week 1 slate (Sep 3-7) into one response -- a team that played
# in both would only show up once if we used ESPN's week number for that range.
ESPN_WEEK_PARAMS = (
    [{"dates": "20260828-20260830"},               # Week 0: Sat Aug 29 openers
     {"dates": "20260903-20260908"}]                # Week 1: Thu 9/3 - Mon 9/7
    + [{"week": i + 2, "seasontype": 2} for i in range(14)]  # Week 2 - Week 15
    + [{"week": 1, "seasontype": 3}]                # Postseason
)

NAME_ALIASES = {
    "massachusetts": "umass",
    "mississippi": "ole miss",
    "ul monroe": "louisiana-monroe",
    "louisiana monroe": "louisiana-monroe",
    "louisiana-lafayette": "louisiana",
    "pitt": "pittsburgh",
    "connecticut": "uconn",
    "nevada-las vegas": "unlv",
    "nevada las vegas": "unlv",
    "florida international": "fiu",
    "florida atlantic": "fau",
    "brigham young": "byu",
    "southern methodist": "smu",
    "texas christian": "tcu",
    "southern california": "usc",
    "texas-el paso": "utep",
    "texas el paso": "utep",
    "texas-san antonio": "utsa",
    "texas san antonio": "utsa",
    "san jose st": "san jose state",
    "app state": "appalachian state",
    "sam houston": "sam houston state",
    "kennesaw st": "kennesaw state",
    "western ky": "western kentucky",
    "wku": "western kentucky",
    "western mi": "western michigan",
    "wmu": "western michigan",
    "eastern mi": "eastern michigan",
    "emu": "eastern michigan",
    "central mi": "central michigan",
    "cmu": "central michigan",
    "niu": "northern illinois",
    "n texas": "north texas",
    "unt": "north texas",
    "s florida": "south florida",
    "usf": "south florida",
    "s alabama": "south alabama",
    "new mexico st": "new mexico state",
    "nmsu": "new mexico state",
    "middle tn": "middle tennessee",
    "mtsu": "middle tennessee",
    "middle tennessee state": "middle tennessee",
    "ga tech": "georgia tech",
    "gt": "georgia tech",
    "boise st": "boise state",
    "fresno st": "fresno state",
    "colorado st": "colorado state",
    "csu": "colorado state",
    "iowa st": "iowa state",
    "utah st": "utah state",
    "usu": "utah state",
    "texas st": "texas state",
    "missouri st": "missouri state",
    "jacksonville st": "jacksonville state",
    "jax state": "jacksonville state",
    "kansas st": "kansas state",
    "k-state": "kansas state",
    "penn st": "penn state",
    "psu": "penn state",
    "arkansas st": "arkansas state",
    "wvu": "west virginia",
    "va tech": "virginia tech",
    "vt": "virginia tech",
    "tamu": "texas a&m",
    "texas am": "texas a&m",
    "ttu": "texas tech",
    "la tech": "louisiana tech",
    "miami-oh": "miami (oh)",
    "miami ohio": "miami (oh)",
    "m-oh": "miami (oh)",
    "north carolina state": "nc state",
    "bc": "boston college",
    "bgsu": "bowling green",
    "odu": "old dominion",
    "coastal caro": "coastal carolina",
    "ecu": "east carolina",
    "jmu": "james madison",
    "florida st": "florida state",
    "w michigan": "western michigan",
    "oregon st": "oregon state",
    "miami oh": "miami (oh)",
    "michigan st": "michigan state",
    "coastal": "coastal carolina",
    "mississippi st": "mississippi state",
    "n illinois": "northern illinois",
    "washington st": "washington state",
    "oklahoma st": "oklahoma state",
    "c michigan": "central michigan",
    "arizona st": "arizona state",
    "ohio st": "ohio state",
    "georgia st": "georgia state",
    "kent st": "kent state",
    "appalachian st": "appalachian state",
    "ga southern": "georgia southern",
    "w kentucky": "western kentucky",
    "e michigan": "eastern michigan",
    "san diego state": "san diego st",
    "n dakota st": "north dakota state",
    "north dakota st": "north dakota state",
    "ndsu": "north dakota state",
    "nc st": "nc state",
    "ball st": "ball state",
    "sacramento st": "sacramento state",
    "s miss": "southern miss",
    "sam houston st": "sam houston state",
}


def normalize_team_name(s):
    n = (s or "").lower().strip()
    n = unicodedata.normalize("NFD", n)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")  # strip accents
    n = re.sub(r"[.']", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return NAME_ALIASES.get(n, n)


def blank_slot():
    return {"opp": "", "fcs": False, "status": "", "score": ""}


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def is_week_complete(schedule, week_idx):
    any_game = False
    for person, teams in schedule.items():
        for team_slots in teams:
            slot = team_slots[week_idx]
            if slot["opp"]:
                any_game = True
                if slot["status"] != "Final":
                    return False
    return any_game


HEADERS = {
    # A real browser UA + referer -- GitHub Actions runners sit on well-known cloud IP
    # ranges that some APIs rate-limit or block for obvious bot/script user agents.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.espn.com/college-football/scoreboard",
}


def fetch_week(week_idx, attempts=3):
    params = ESPN_WEEK_PARAMS[week_idx]
    if "dates" in params:
        qs = f"dates={params['dates']}"
    else:
        qs = f"week={params['week']}&seasontype={params['seasontype']}&year=2026"
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&{qs}&limit=100"

    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, timeout=20, headers=HEADERS)
            resp.raise_for_status()
            data = resp.json()
            if "events" not in data:
                # ESPN sometimes returns HTTP 200 with an error payload instead of a real
                # HTTP error status -- raise_for_status() doesn't catch this, so check for it
                # explicitly rather than silently treating it as "zero games this week".
                raise RuntimeError(f"ESPN response had no 'events' key: {data}")
            return data["events"]
        except Exception as e:
            last_err = e
            if attempt < attempts:
                time.sleep(2 * attempt)  # 2s, then 4s
    raise last_err


def main():
    with open(DRAFT_PATH) as f:
        draft = json.load(f)
    people = list(draft.keys())

    team_owners = {}
    for person in people:
        for idx, team in enumerate(draft[person]):
            key = normalize_team_name(team)
            team_owners.setdefault(key, []).append((person, idx))

    # Load whatever's already there so a failed week doesn't wipe prior good data, and so
    # manual Commissioner overrides for teams ESPN doesn't have (e.g. FCS opponents) survive.
    schedule = load_json(SCHEDULE_PATH, {})
    schedule = schedule.get("data", schedule) if isinstance(schedule, dict) else {}
    for person in people:
        if person not in schedule:
            schedule[person] = [[blank_slot() for _ in WEEKS] for _ in draft[person]]
        else:
            for idx in range(len(draft[person])):
                if idx >= len(schedule[person]):
                    schedule[person].append([blank_slot() for _ in WEEKS])
                while len(schedule[person][idx]) < len(WEEKS):
                    schedule[person][idx].append(blank_slot())

    changed_weeks = []
    failed_weeks = []
    attempted_weeks = 0
    for week_idx, week_label in enumerate(WEEKS):
        if is_week_complete(schedule, week_idx):
            print(f"skip {week_label}: already complete")
            continue
        attempted_weeks += 1
        try:
            events = fetch_week(week_idx)
        except Exception as e:
            print(f"WARN: fetch failed for {week_label}: {e}", file=sys.stderr)
            failed_weeks.append(week_label)
            continue

        fresh = {
            person: [dict(schedule[person][idx][week_idx]) for idx in range(len(draft[person]))]
            for person in people
        }

        for ev in events:
            comps = ev.get("competitions") or [{}]
            competitors = comps[0].get("competitors") or []
            if len(competitors) < 2:
                continue
            teams = []
            for c in competitors:
                t = c.get("team", {})
                names = [n for n in [t.get("displayName"), t.get("shortDisplayName"), t.get("location"), t.get("name")] if n]
                teams.append({"names": names, "score": c.get("score")})
            status = ev.get("status", {}).get("type", {})
            completed = bool(status.get("completed"))
            status_detail = status.get("shortDetail", "")

            side_owners = []
            for t in teams:
                found = None
                for nm in t["names"]:
                    key = normalize_team_name(nm)
                    if key in team_owners:
                        found = {"owners": team_owners[key], "name": t["names"][1] if len(t["names"]) > 1 else t["names"][0]}
                        break
                side_owners.append(found)

            for ti, t in enumerate(teams):
                mine = side_owners[ti]
                if not mine:
                    continue
                other = side_owners[1 - ti]
                opp_names = teams[1 - ti]["names"]
                opp_display = opp_names[1] if len(opp_names) > 1 else opp_names[0]
                for person, idx in mine["owners"]:
                    fresh[person][idx] = {
                        "opp": opp_display,
                        "fcs": not other,
                        "status": "Final" if completed else "Scheduled",
                        "score": f"{t['score']}-{teams[1-ti]['score']}" if completed else "",
                    }

        for person in people:
            for idx in range(len(draft[person])):
                schedule[person][idx][week_idx] = fresh[person][idx]
        changed_weeks.append(week_label)
        print(f"synced {week_label}: {len(events)} events")
        time.sleep(0.3)  # be a polite guest

    if attempted_weeks > 0 and len(failed_weeks) == attempted_weeks:
        # Every week we tried to fetch failed -- ESPN is down, blocking us, or the URL/params
        # are wrong. Don't write a file with a fresh "updated_at" timestamp and zero real
        # changes; that would look like a successful sync in the app when nothing actually
        # synced. Exit with an error instead so the GitHub Action run shows red.
        print(f"ERROR: all {attempted_weeks} attempted week(s) failed to fetch: {failed_weeks}", file=sys.stderr)
        sys.exit(1)

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "changed_weeks": changed_weeks,
        "failed_weeks": failed_weeks,
        "data": schedule,
    }
    os.makedirs(os.path.dirname(SCHEDULE_PATH), exist_ok=True)
    with open(SCHEDULE_PATH, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {SCHEDULE_PATH} -- {len(changed_weeks)} week(s) updated: {changed_weeks}")
    if failed_weeks:
        print(f"NOTE: {len(failed_weeks)} week(s) failed and were left as-is: {failed_weeks}", file=sys.stderr)


if __name__ == "__main__":
    main()
