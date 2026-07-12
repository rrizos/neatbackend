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
  export GROQ_API_KEY=...          # from https://console.groq.com/keys
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
import groq

ATHENS_TZ = ZoneInfo("Europe/Athens")

API_BASE = os.environ.get("NEAT_API_BASE", "http://63.181.201.175")
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Set to a seed username (or substring, e.g. "athina") to force exactly one
# post for that city right now, bypassing the taper check and the random
# hourly gate. For manual end-to-end testing only — leave unset for cron.
FORCE_CITY = os.environ.get("FORCE_CITY", "").strip().lower()

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
Οι αναρτήσεις σε τοπικό feed γειτονιάς είναι ατημέλητες και γραμμένες βιαστικά
στο κινητό — ΟΧΙ γυαλισμένο, τέλεια δομημένο κείμενο. Ξεκινούν συχνά με πεζό
γράμμα, δεν έχουν πάντα τελεία στο τέλος, χρησιμοποιούν λέξεις όπως "ρε",
"τελικά", "παιδιά", "βρε". Emoji σπάνια, ΠΟΤΕ hashtags, ΠΟΤΕ διαφημιστικό ύφος.

Παραδείγματα πραγματικού ύφους (σημείωσε πόσο διαφορετική είναι η δομή σε κάθε ένα):
- "ρε παιδιά ξέρει κανείς γιατί έκλεισε ο δρόμος στην κεντρική; έχει μποτιλιάρισμα απίστευτο"
- "μόλις είδα ένα σκυλάκι χαμένο κοντά στο πάρκο, έχει μπλε κολάρο αν το αναγνωρίζει κανείς"
- "Τελικά άνοιξε το μαγαζί με τα τσουρέκια, δοκιμάστε το πραγματικά αξίζει"
- "καμιά σύσταση για υδραυλικό; έχω θέμα με τον θερμοσίφωνα και δεν ξέρω ποιον να πάρω"
- "τρίτη μέρα χωρίς νερό στη γειτονιά μου, κανείς άλλος;"
- "βρήκα φοβερή θέα από την ταράτσα χθες, δεν φεύγω ποτέ από δω 😅"
- "επιτέλους άνοιξε ο δρόμος, χτες ήταν χάος"
- "κανείς δεν είδε τι έγινε έξω από το σχολείο σήμερα το πρωί; είχε πολλή φασαρία"
- "ψάχνω κάποιον να μου πει καλό μέρος για delivery, βαρέθηκα τα ίδια"
- "τι ώρα κλείνει το περίπτερο απόψε ξέρει κανείς"
- "βγήκε κανείς βόλτα το απόγευμα; έχει τέλειο καιρό σήμερα"
- "τρελό μποτιλιάρισμα σήμερα, μισή ώρα στο ίδιο σημείο"
- "νέο περίπτερο δίπλα στο φούρνο, έχει και παγωτά επιτέλους"
- "έχει κανείς εμπειρία με το νέο γυμναστήριο; αξίζει η συνδρομή"
- "κανείς γείτονας ξύπνιος; μόλις άκουσα φοβερό θόρυβο απ' έξω"
- "αν ξέρει κανείς καλό κομμωτήριο ας πει, ψάχνω εδώ και μέρες"
- "τελικά η καφετέρια στη γωνία έκλεισε; πέρασα σήμερα και ήταν κλειστά τα ρολά"
""".strip()

# Force variety in structure across calls instead of always defaulting to the
# same "helpful question" pattern the model gravitates to.
POST_MOODS = [
    "ερώτηση προς τη γειτονιά",
    "απλή παρατήρηση ή σχόλιο για κάτι που είδες",
    "μικρό παράπονο",
    "σύσταση ή θετικό σχόλιο",
    "γρήγορη αντίδραση σε κάτι που μόλις συνέβη",
]

_client = groq.Groq(api_key=GROQ_API_KEY)


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
    mood = random.choice(POST_MOODS)
    if examples:
        style_block = "Παραδείγματα πρόσφατων πραγματικών posts (μίμησε το ύφος, ΟΧΙ το περιεχόμενο):\n" + \
            "\n".join(f"- {t}" for t in examples)
    else:
        style_block = DEFAULT_STYLE_GUIDE

    prompt = f"""
Γράψε ΜΙΑ ανάρτηση για ένα τοπικό feed γειτονιάς στην πόλη {city['name']}, Ελλάδα.
Θέμα: {topic}.
Τόνος αυτής της συγκεκριμένης ανάρτησης: {mood}.

{style_block}

Κανόνες:
- 1-2 προτάσεις, σαν πραγματικός κάτοικος βιαστικά στο κινητό, ΟΧΙ γυαλισμένο κείμενο.
- Μπορεί να ξεκινά με πεζό γράμμα, μπορεί να μην έχει τελεία στο τέλος.
- ΜΗΝ ξεκινάς πάντα με "Ξέρει κανείς" — ταίριαξε τη δομή με τον τόνο που σου δόθηκε.
- Το πολύ 1 emoji, συνήθως κανένα. Χωρίς hashtags, χωρίς διαφημιστικό ή εξυπηρετικό τόνο chatbot.
- Απάντησε ΜΟΝΟ με το κείμενο της ανάρτησης, τίποτα άλλο, καμία εισαγωγή.
""".strip()

    completion = None
    for attempt in range(3):
        try:
            completion = _client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=1.05,
                max_tokens=200,
                # gpt-oss is a reasoning model: "low" keeps it fast/cheap for a one-line
                # post (no multi-step reasoning needed), and "hidden" keeps the chain-of-
                # thought out of message.content so it can't leak into the actual post text.
                reasoning_effort="low",
                reasoning_format="hidden",
            )
            break
        except (groq.RateLimitError, groq.InternalServerError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))  # transient overload — brief backoff and retry

    text = (completion.choices[0].message.content or "").strip().strip('"')
    # Defensive: some reasoning models occasionally leak <think>...</think> blocks into
    # content even with reasoning_format="hidden" — strip them rather than post them.
    if "<think>" in text and "</think>" in text:
        text = (text.split("</think>", 1)[1]).strip().strip('"')
    if not text:
        finish_reason = completion.choices[0].finish_reason
        raise ValueError(f"Groq returned empty text (finish_reason={finish_reason})")
    return text


def create_post(token: str, text: str) -> None:
    # The backend only takes the multipart-upload code path when the request
    # Content-Type actually says multipart/form-data — plain `data=` sends
    # application/x-www-form-urlencoded instead, so force it via `files=`.
    resp = requests.post(
        f"{API_BASE}/api/posts/",
        headers={"Authorization": f"Token {token}"},
        files={"text": (None, text), "media": (None, "[]")},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code} error creating post: {resp.text}")


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
            forced = bool(FORCE_CITY) and FORCE_CITY in city["username"].lower()

            real_count = real_posts_last_24h(city, seed_usernames)
            if real_count >= TAPER_THRESHOLD and not forced:
                print(f"[{city['name']}] {real_count} real posts/24h — seeding paused")
                continue

            if not forced and not should_post_now(account):
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
            # One city's failure (e.g. a transient Groq/network error) must
            # not stop account creation or posting for the remaining cities.
            print(f"[{city['name']}] ERROR: {exc}")
            continue


if __name__ == "__main__":
    run_once()
