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
import urllib.request
import urllib.parse
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
    """Print to console and send alert directly to owner's Telegram."""
    print(owner_msg)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("ℹ️  Telegram bot token or chat ID not set, skipping Telegram notification.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": owner_msg,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                print("📱 Alert sent to Telegram!")
    except Exception as e:
        print(f"⚠️ Failed to send Telegram message: {e}")


def generate_summary(reviews, results):
    """
    Produce a category ranking report sorted by frequency and severity.
    """
    category_counts = {c: 0 for c in CATEGORIES}
    category_weights = {c: 0 for c in CATEGORIES}

    for r in reviews:
        analysis = results.get(r["id"])
        if not analysis:
            continue
        for cat in analysis["categories"]:
            category_counts[cat] += 1
            category_weights[cat] += (analysis["severity"] + 1)

    # Sort categories by weighted impact
    ranked = sorted(
        [(cat, category_counts[cat], category_weights[cat]) for cat in CATEGORIES if category_counts[cat] > 0],
        key=lambda x: x[2],
        reverse=True
    )

    if not ranked:
        return "📊 Summary: No complaints recorded in this batch!"

    summary_lines = ["📊 *TOP COMPLAINT AREAS (Fix This First)*:\n"]
    for i, (cat, count, weight) in enumerate(ranked, 1):
        summary_lines.append(f"{i}. *{cat.capitalize()}*: {count} complaint(s) (impact score: {weight})")

    return "\n".join(summary_lines)


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
        stars = "⭐" * r["rating"]
        severity_label = {0: "Low", 1: "⚠️ Medium", 2: "🚨 URGENT"}[a["severity"]]

        msg = (
            f"🚨 NEW URGENT REVIEW\n\n"
            f"Rating: {stars} ({r['rating']}/5)\n"
            f"Severity: {severity_label}\n"
            f"Categories: {', '.join(a['categories']) or 'none'}\n\n"
            f"Review:\n{r['text'] or '(star-only review, no text)'}\n\n"
            f"💬 DRAFT REPLY:\n{a['reply'] or '(no reply needed)'}"
        )
        notify(msg)

    # Print & notify the ranked summary
    summary_msg = generate_summary(new, results)
    print("\n" + summary_msg)
    notify(summary_msg)

    save_seen(seen | {r["id"] for r in reviews})


if __name__ == "__main__":
    main()
