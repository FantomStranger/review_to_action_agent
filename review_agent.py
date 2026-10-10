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


# ---- 1. source: replace with the live Apify call before the demo -------------
def load_raw(path="reviews.json"):
    with open(path, encoding="utf-8") as f:
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
    raise NotImplementedError("plug in Gemini/Groq/etc. here")


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
