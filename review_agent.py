"""
review_agent.py  -  reviews -> normalize -> LLM tag/severity/draft -> urgent set U -> alert

Spec (see chat):
  R     = fetched reviews; r has id, rating in 1..5, text (possibly empty)
  C     = {food, service, price, ambience, wait, hygiene, billing}
  LLM   : tag(r) subset of C, sev(r) in {0,1,2}, draft(r)   (only if text(r) != "")
  U     = {r in R | rating(r) <= 2  or  sev(r) = 2}
  New   = R minus Seen ; Seen' = Seen U R
  notify(owner, U ∩ New) <=> U ∩ New != {}
"""
import json
import os
import sys
from dotenv import load_dotenv
from groq import Groq
from apify_client import ApifyClient

# Ensure UTF-8 output on Windows terminal
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

CATEGORIES = ["food", "service", "price", "ambience", "wait", "hygiene", "billing"]
SEEN_FILE = "seen.json"

PROMPT = """You analyse one Google review of a cafe/restaurant. Reply with JSON only, no other text:
{{"categories": [...], "severity": 0, "reply": "..."}}

categories: any of {cats}. Only what the review text actually mentions.
severity: 0 = praise or trivial, 1 = real complaint, 2 = urgent (health, hygiene, allergy,
          food safety, or the customer says they were overcharged / cheated).
reply: a short, polite public reply from the owner (max 60 words). Rules: thank them, do not
       admit fault for disputed facts, do not promise refunds or compensation, invite them
       to contact the cafe directly if there is a problem.

Star rating: {rating}
Review: {text}
"""


# ---- 1. source: LIVE Apify Google Maps Reviews Scraper -----------------------
def load_raw():
    """
    Fetch live Google reviews via Apify.
    Fallback to reviews.json if Apify fails (so demo never crashes).
    """
    try:
        client = ApifyClient(os.environ.get("APIFY_API_TOKEN"))

        # Start the actor with the exact Place ID for Mana Katha Cafe & Restaurant
        print("📡 Fetching live reviews from Google Maps via Apify...")
        run = client.actor("compass/google-maps-reviews-scraper").call(
            run_input={
                "placeIds": ["ChIJe2giTQChyzsRVTHYZV1Hc_8"],
                "maxReviews": 20,
                "language": "en",
            }
        )

        # Get the results from the dataset
        dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "default_dataset_id", getattr(run, "defaultDatasetId", None))
        items = list(client.dataset(dataset_id).iterate_items())

        if not items:
            print("⚠️  No items returned by live scraper, using cached dataset")
            with open("reviews.json", encoding="utf-8") as f:
                return json.load(f)

        print(f"✅ Fetched {len(items)} live reviews from Google Maps")
        return items

    except Exception as e:
        print(f"⚠️  Apify failed ({e}), falling back to reviews.json")
        with open("reviews.json", encoding="utf-8") as f:
            return json.load(f)


# ---- 2. normalize: everything downstream sees ONLY these three fields ---------
def normalize(raw):
    return {
        "id": raw["reviewUrl"],
        "rating": int(raw["stars"]),
        "text": (raw.get("text") or "").strip(),
    }


# ---- 3. the one function you plug your LLM provider into ---------------------
def call_llm(prompt: str) -> str:
    """Send prompt to your LLM provider, return its text reply."""
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return response.choices[0].message.content


def analyse(r):
    if not r["text"]:  # star-only review: nothing to tag, nothing to reply to
        return {"categories": [], "severity": 0, "reply": ""}
    out = call_llm(PROMPT.format(cats=CATEGORIES, rating=r["rating"], text=r["text"]))
    data = json.loads(out[out.index("{"): out.rindex("}") + 1])
    data["categories"] = [c for c in data.get("categories", []) if c in CATEGORIES]
    data["severity"] = min(max(int(data.get("severity", 0)), 0), 2)
    data["reply"] = data.get("reply", "")
    return data


# ---- 4. plain-Python logic: no LLM involved ----------------------------------
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)


def notify(owner_msg: str):
    print(owner_msg)  # next step: send via Telegram bot


def main():
    reviews = [normalize(x) for x in load_raw()]
    seen = load_seen()
    new = [r for r in reviews if r["id"] not in seen]

    results = {r["id"]: analyse(r) for r in new}
    urgent = [r for r in new if r["rating"] <= 2 or results[r["id"]]["severity"] == 2]

    if urgent:
        print(f"\n🚨 Found {len(urgent)} urgent reviews:\n")

    for r in urgent:
        a = results[r["id"]]
        notify(
            f"[{r['rating']}*] {r['text'] or '(no text)'}\n"
            f"  tags={a['categories']} severity={a['severity']}\n"
            f"  draft reply: {a['reply'] or '(none - star-only review)'}\n"
        )

    save_seen(seen | {r["id"] for r in reviews})


if __name__ == "__main__":
    main()
