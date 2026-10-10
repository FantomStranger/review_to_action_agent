"""
review_agent.py  -  Cafe Review-to-Action Agent

Modes:
  1. Default: Single-pass live ingest & urgent notification
  2. --watch: Continuous 24/7 background polling for new reviews
  3. --report: Comprehensive historical audit and category breakdown
"""
import argparse
import json
import os
import sys
import time
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
def load_raw(max_reviews=20):
    """
    Fetch live Google reviews via Apify.
    Fallback to reviews.json if Apify fails (so demo never crashes).
    """
    try:
        client = ApifyClient(os.environ.get("APIFY_API_TOKEN"))

        print(f"📡 Fetching live reviews from Google Maps via Apify (limit={max_reviews})...")
        run = client.actor("compass/google-maps-reviews-scraper").call(
            run_input={
                "placeIds": ["ChIJe2giTQChyzsRVTHYZV1Hc_8"],
                "maxReviews": max_reviews,
                "language": "en",
            }
        )

        dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "default_dataset_id", getattr(run, "defaultDatasetId", None))
        items = list(client.dataset(dataset_id).iterate_items())

        if not items:
            print("⚠️  No items returned by live scraper, using cached dataset")
            with open("reviews.json", encoding="utf-8") as f:
                return json.load(f)[:max_reviews]

        print(f"✅ Fetched {len(items)} live reviews from Google Maps")
        return items

    except Exception as e:
        print(f"⚠️  Apify failed ({e}), falling back to reviews.json")
        with open("reviews.json", encoding="utf-8") as f:
            return json.load(f)[:max_reviews]


# ---- 2. normalize: everything downstream sees ONLY these three fields ---------
def normalize(raw):
    return {
        "id": raw.get("reviewUrl") or raw.get("id") or str(raw.get("stars")),
        "rating": int(raw.get("stars", 5)),
        "text": (raw.get("text") or "").strip(),
    }


# ---- 3. LLM analysis via Groq ------------------------------------------------
def call_llm(prompt: str) -> str:
    """Send prompt to Groq LLM (openai/gpt-oss-120b), return its text reply."""
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


# ---- 4. State & Persistence (Deduplication) -----------------------------------
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


# ---- 5. Operational Pipelines ------------------------------------------------
def run_single_pass():
    """Single-pass live fetch, deduplication, and urgent alert notification."""
    reviews = [normalize(x) for x in load_raw()]
    seen = load_seen()
    new = [r for r in reviews if r["id"] not in seen]

    if not new:
        print("✅ No new reviews since last run.")
        return

    print(f"🔍 Analyzing {len(new)} new review(s)...")
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

    summary_msg = generate_summary(new, results)
    print("\n" + summary_msg)
    notify(summary_msg)

    save_seen(seen | {r["id"] for r in reviews})


def run_watch_mode(interval=30):
    """Mode 1: Continuous 24/7 background monitor loop."""
    print(f"🟢 [MODE 1: LIVE WATCHDOG] Starting continuous monitoring (polling every {interval}s)...")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            print(f"[{time.strftime('%H:%M:%S')}] Polling for new reviews...")
            run_single_pass()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n🛑 Watchdog stopped by user.")


def run_report_mode():
    """Mode 2: Comprehensive historical audit and analytics."""
    print("📊 [MODE 2: HISTORICAL AUDIT] Running deep analysis on cafe reviews...\n")
    reviews = [normalize(x) for x in load_raw(max_reviews=50)]

    total = len(reviews)
    if total == 0:
        print("No reviews found.")
        return

    avg_rating = sum(r["rating"] for r in reviews) / total
    rating_dist = {i: sum(1 for r in reviews if r["rating"] == i) for i in range(1, 6)}

    print(f"🔍 Analyzing all {total} reviews with Groq LLM...")
    results = {r["id"]: analyse(r) for r in reviews}

    ranking = generate_summary(reviews, results)

    report = (
        f"📋 *MANA KATHA CAFE - EXECUTIVE AUDIT REPORT*\n"
        f"═════════════════════════════════════════\n"
        f"📈 *Overall Health*:\n"
        f"• Total Reviews Analyzed: {total}\n"
        f"• Average Rating: {avg_rating:.2f} / 5.0 ⭐\n"
        f"• 5-Star: {rating_dist[5]} | 4-Star: {rating_dist[4]} | 3-Star: {rating_dist[3]}\n"
        f"• 2-Star: {rating_dist[2]} | 1-Star: {rating_dist[1]}\n\n"
        f"{ranking}\n\n"
        f"💡 *Action Recommendation*:\n"
        f"Focus kitchen and staff training on top-ranked categories to prevent customer churn."
    )

    print("\n" + report)
    notify(report)


def main():
    parser = argparse.ArgumentParser(description="Cafe Review-to-Action Agent")
    parser.add_argument("--watch", action="store_true", help="Run in continuous 24/7 live monitoring mode")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds for watch mode (default: 30)")
    parser.add_argument("--report", action="store_true", help="Run comprehensive historical audit report mode")

    args = parser.parse_args()

    if args.watch:
        run_watch_mode(interval=args.interval)
    elif args.report:
        run_report_mode()
    else:
        run_single_pass()


if __name__ == "__main__":
    main()
