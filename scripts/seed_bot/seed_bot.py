#!/usr/bin/env python3
"""
Neat seed-content bot.

Posts locally-flavored filler content in 10 Greek cities so the feed isn't
empty at launch. Run this once per hour via cron; each tick decides
per-city, with a randomized human-like probability, whether to post.

As real users start posting in a city, `real_posts_last_24h` rises and
`SEED_TAPER_THRESHOLD` throttles seed posting toward zero automatically.

Setup:
  pip install -r requirements.txt
  export GEMINI_API_KEY=...        # from https://aistudio.google.com/apikey
  python seed_bot.py               # creates the 10 seed accounts on first run
  # then add to crontab: 0 * * * * cd .../seed_bot && python seed_bot.py
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google import genai

ATHENS_TZ = ZoneInfo("Europe/Athens")

API_BASE = os.environ.get("NEAT_API_BASE", "http://63.181.201.175")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = "gemini-2.0-flash"

STATE_FILE = Path(__file__).parent / "seed_state.json"

# Target seed posts/day per city while a city is still "cold". Real
# activity tapers this toward zero — see real_posts_last_24h() below.
TARGET_POSTS_PER_DAY = 3
TAPER_THRESHOLD = 5          # real posts/24h at which seeding stops entirely
MIN_GAP_HOURS = 3            # don't let two seed posts land closer than this
ACTIVE_HOURS = range(8, 24)  # only post 08:00-23:59 local, like a person would

CITIES = [
    {"name": "Αθήνα", "username": "neat_local_athina", "topics": [
        "κυκλοφοριακό μποτιλιάρισμα σε κεντρικό δρόμο",
        "νέο καφέ που άνοιξε στη γειτονιά",
        "συζήτηση για τα σκουπίδια/καθαριότητα",
        "σύσταση για βόλτα το απόγευμα",
        "ερώτηση για κάποιο μαγαζί/κατάστημα της περιοχής",
    ]},
    {"name": "Θεσσαλονίκη", "username": "neat_local_thessaloniki", "topics": [
        "βόλτα στην παραλία / Νέα Παραλία",
        "κίνηση στο κέντρο",
        "νέο μαγαζί ή καφέ",
        "ερώτηση για σύσταση φαγητού",
        "καιρός/θέα από μπαλκόνι",
    ]},
    {"name": "Πάτρα", "username": "neat_local_patra", "topics": [
        "βόλτα στην παραλιακή",
        "κίνηση στο κέντρο της πόλης",
        "ερώτηση για καφετέρια/ταβέρνα",
        "κάτι που συνέβη στη γειτονιά",
    ]},
    {"name": "Ηράκλειο", "username": "neat_local_irakleio", "topics": [
        "βόλτα στο λιμάνι",
        "ζέστη/καιρός",
        "σύσταση για ταβέρνα",
        "κάτι τοπικό που συμβαίνει σήμερα",
    ]},
    {"name": "Λάρισα", "username": "neat_local_larisa", "topics": [
        "βόλτα στο πάρκο Αλκαζάρ",
        "κίνηση στο κέντρο",
        "ερώτηση για μαγαζί της περιοχής",
    ]},
    {"name": "Βόλος", "username": "neat_local_volos", "topics": [
        "βόλτα στην παραλία",
        "τσιπουράδικο/σύσταση φαγητού",
        "θέα στο Πήλιο",
    ]},
    {"name": "Ιωάννινα", "username": "neat_local_ioannina", "topics": [
        "βόλτα στη λίμνη",
        "κρύο/καιρός",
        "σύσταση για καφέ στην παλιά πόλη",
    ]},
    {"name": "Χανιά", "username": "neat_local_chania", "topics": [
        "βόλτα στο ενετικό λιμάνι",
        "ζέστη/καιρός",
        "σύσταση για ταβέρνα στα Χανιά",
    ]},
    {"name": "Ρόδος", "username": "neat_local_rodos", "topics": [
        "βόλτα στην παλιά πόλη",
        "τουρίστες/καιρός",
        "σύσταση για παραλία",
    ]},
    {"name": "Καβάλα", "username": "neat_local_kavala", "topics": [
        "βόλτα στο κάστρο/παλιά πόλη",
        "θέα στο λιμάνι",
        "σύσταση για καφέ",
    ]},
]

DEFAULT_STYLE_GUIDE = """
Τυπικές αναρτήσεις σε τοπικό feed γειτονιάς είναι σύντομες (1-2 προτάσεις),
ανεπίσημες, γραμμένες σαν να τις έγραψε κάτοικος της περιοχής στο κινητό του.
Χρησιμοποιούν καθημερινή γλώσσα, καμιά φορά ένα emoji, ποτέ hashtags,
ποτέ διαφημιστικό ύφος. Παράδειγμα ύφους:
- "Ξέρει κανείς αν άνοιξε το καφέ στη γωνία; Πέρασα χθες και έδειχνε έτοιμο"
- "Βόλτα στο κέντρο τώρα, έχει τρελή ζέστη σήμερα ☀️"
- "Κανείς άλλος κολλημένος στην κίνηση στη Μητροπόλεως;"
""".strip()

_client = genai.Client(api_key=GEMINI_API_KEY)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"accounts": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def ensure_account(state: dict, city: dict) -> dict:
    """Create the seed account for this city on first run; reuse it after."""
    existing = state["accounts"].get(city["username"])
    if existing:
        return existing

    password = f"neat-seed-{random.randint(10**8, 10**9 - 1)}"
    signup = requests.post(
        f"{API_BASE}/api/auth/signup/",
        json={
            "username": city["username"],
            "password": password,
            "email": f"{city['username']}@neat-seed.local",
            "fullName": f"Neat {city['name']}",
        },
        timeout=30,
    )
    signup.raise_for_status()
    token = signup.json()["token"]

    patch = requests.patch(
        f"{API_BASE}/api/auth/me/",
        headers={"Authorization": f"Token {token}"},
        json={"city": city["name"], "bio": "Τοπικές ενημερώσεις της περιοχής 🏙️"},
        timeout=30,
    )
    patch.raise_for_status()

    account = {
        "username": city["username"],
        "password": password,
        "token": token,
        "post_count": 0,
        "last_post_at": None,
    }
    state["accounts"][city["username"]] = account
    save_state(state)
    return account


def real_posts_last_24h(city: dict, seed_usernames: set[str]) -> int:
    resp = requests.get(
        f"{API_BASE}/api/posts/",
        params={"city": city["name"]},
        timeout=30,
    )
    resp.raise_for_status()
    posts = resp.json()
    if isinstance(posts, dict):
        posts = posts.get("results", [])
    count = 0
    for p in posts:
        if p.get("author") in seed_usernames:
            continue
        if p.get("minutesAgo", 10**9) < 24 * 60:
            count += 1
    return count


def recent_real_examples(city: dict, seed_usernames: set[str], limit: int = 5) -> list[str]:
    resp = requests.get(
        f"{API_BASE}/api/posts/",
        params={"city": city["name"]},
        timeout=30,
    )
    resp.raise_for_status()
    posts = resp.json()
    if isinstance(posts, dict):
        posts = posts.get("results", [])
    texts = [
        p["text"] for p in posts
        if p.get("author") not in seed_usernames and p.get("text")
    ]
    return texts[:limit]


def generate_post_text(city: dict, examples: list[str]) -> str:
    topic = random.choice(city["topics"])
    if examples:
        style_block = "Παραδείγματα πρόσφατων πραγματικών posts (μίμησε το ύφος, ΟΧΙ το περιεχόμενο):\n" + \
            "\n".join(f"- {t}" for t in examples)
    else:
        style_block = DEFAULT_STYLE_GUIDE

    prompt = f"""
Γράψε ΜΙΑ ανάρτηση για ένα τοπικό feed γειτονιάς στην πόλη {city['name']}, Ελλάδα.
Θέμα: {topic}.

{style_block}

Κανόνες:
- 1-2 προτάσεις, ανεπίσημο ύφος, σαν πραγματικός κάτοικος.
- Το πολύ 1 emoji, μπορεί και κανένα.
- Χωρίς hashtags, χωρίς διαφημιστικό τόνο, χωρίς "ως AI".
- Απάντησε ΜΟΝΟ με το κείμενο της ανάρτησης, τίποτα άλλο.
""".strip()

    response = _client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text.strip().strip('"')


def create_post(token: str, text: str) -> None:
    resp = requests.post(
        f"{API_BASE}/api/posts/",
        headers={"Authorization": f"Token {token}"},
        data={"text": text, "media": "[]"},
        timeout=30,
    )
    resp.raise_for_status()


def should_post_now(account: dict) -> bool:
    now = datetime.now(timezone.utc)
    if now.astimezone(ATHENS_TZ).hour not in ACTIVE_HOURS:
        return False
    if account["last_post_at"]:
        last = datetime.fromisoformat(account["last_post_at"])
        if (now - last).total_seconds() < MIN_GAP_HOURS * 3600:
            return False
    # Roughly TARGET_POSTS_PER_DAY posts spread across ACTIVE_HOURS ticks.
    hourly_chance = TARGET_POSTS_PER_DAY / len(ACTIVE_HOURS)
    return random.random() < hourly_chance


def run_once() -> None:
    state = load_state()
    seed_usernames = {c["username"] for c in CITIES}

    for city in CITIES:
        try:
            account = ensure_account(state, city)

            real_count = real_posts_last_24h(city, seed_usernames)
            if real_count >= TAPER_THRESHOLD:
                print(f"[{city['name']}] {real_count} real posts/24h — seeding paused")
                continue

            if not should_post_now(account):
                continue

            examples = recent_real_examples(city, seed_usernames)
            text = generate_post_text(city, examples)
            create_post(account["token"], text)

            account["post_count"] += 1
            account["last_post_at"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            print(f"[{city['name']}] posted: {text}")

            time.sleep(random.uniform(1, 4))  # small human-like gap between cities
        except Exception as exc:
            # One city's failure (e.g. a transient Gemini/network error) must
            # not stop account creation or posting for the remaining cities.
            print(f"[{city['name']}] ERROR: {exc}")
            continue


if __name__ == "__main__":
    run_once()
