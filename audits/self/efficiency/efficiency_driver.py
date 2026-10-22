#!/usr/bin/env python3
"""Harness efficiency experiment: 10 efforts x small budget, many tiny requests via
`harness ask` through the router's cheapest-capable paid path.

Rerunnable. Reads nothing from the operator's own harness config: HOME and
XDG_CONFIG_HOME are redirected into the work dir, so no real ledger/config is
touched and no key is stored anywhere.

Environment:
  OPENROUTER_API_KEY   your key (required; never stored, used only in-process)
  HARNESS_USE_FREE=0   required for paid routing (:free models 429-flake)
  HARNESS_EFFICIENCY_REPO   repo root (default: derived from this file's location)
  HARNESS_EFFICIENCY_WORK   work dir (default: ./efficiency-results)
  HARNESS_EFFORT_CAP       per-effort spend cap (default $0.008)
  HARNESS_TOTAL_GUARD      total spend guard (default $0.09)
"""
import os, sys, json, subprocess, time, urllib.request

KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
if not KEY:
    sys.exit("set OPENROUTER_API_KEY first")
if os.environ.get("HARNESS_USE_FREE", "1") != "0":
    sys.exit("set HARNESS_USE_FREE=0 for paid routing (free tier 429-flakes)")

REPO = os.environ.get(
    "HARNESS_EFFICIENCY_REPO",
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))  # audits/self/efficiency -> repo root
WORK = os.environ.get("HARNESS_EFFICIENCY_WORK",
                      os.path.join(os.getcwd(), "efficiency-results"))
os.makedirs(WORK, exist_ok=True)
RESULTS = os.path.join(WORK, "results.jsonl")
PY = sys.executable
EFFORT_CAP = float(os.environ.get("HARNESS_EFFORT_CAP", "0.008"))
TOTAL_GUARD = float(os.environ.get("HARNESS_TOTAL_GUARD", "0.09"))

SANDBOX_HOME = os.path.join(WORK, "sandbox-home")
BASE_ENV = dict(os.environ, HOME=SANDBOX_HOME,
                XDG_CONFIG_HOME=os.path.join(SANDBOX_HOME, ".config"),
                OPENROUTER_API_KEY=KEY, NO_COLOR="1", PYTHONPATH=REPO,
                HARNESS_USE_FREE="0")

def key_usage():
    req = urllib.request.Request("https://openrouter.ai/api/v1/auth/key",
                                 headers={"Authorization": "Bearer " + KEY})
    d = json.load(urllib.request.urlopen(req, timeout=30))["data"]
    return float(d.get("limit") or 0), float(d.get("usage") or 0)

def ask2(prompt):
    t0 = time.time()
    r = subprocess.run(
        [PY, "-c", "import sys; sys.argv=['harness','ask','--quiet']+sys.argv[1:]; "
                   "from harness.cli import main; main()",
         prompt],
        capture_output=True, text=True, env=BASE_ENV, cwd=REPO, timeout=240)
    return r, time.time() - t0

def run_one(prompt, check="basic"):
    for attempt in range(2):
        r, dt = ask2(prompt)
        try:
            d = json.loads(r.stdout)
        except Exception:
            d = {}
        status = d.get("status")
        if status == "ok":
            resp = (d.get("response") or "").strip()
            ok = bool(resp) and "could not complete" not in resp.lower()
            if ok and check == "json":
                try:
                    s = resp
                    i, j = s.find("{"), s.rfind("}")
                    json.loads(s[i:j + 1] if i >= 0 and j > i else s)
                except Exception:
                    ok = False
            return {"ok": ok, "model": d.get("model"), "cost": d.get("cost", 0),
                    "latency": round(dt, 1), "resp": resp[:300],
                    "attempts": attempt + 1}
        if status == "deferred" and "429" in str(d.get("defer_reason")) and attempt == 0:
            time.sleep(15)
            continue
        return {"ok": False, "model": d.get("model"), "cost": d.get("cost", 0),
                "latency": round(dt, 1),
                "resp": (d.get("response") or d.get("defer_reason") or "")[:200],
                "attempts": attempt + 1, "status": status}
    return {"ok": False, "model": None, "cost": 0, "latency": 0,
            "resp": "retry-exhausted", "attempts": 2}

EFFORTS = [
 ("factual-qa", "basic", [
  "What is 6 times 7? Answer with just the number.",
  "What is the capital of Japan? One word.",
  "How many days are in a leap year? Number only.",
  "What gas do plants absorb? One word.",
  "What is 15% of 200? Number only.",
  "Which planet is the Red Planet? One word.",
  "Chemical symbol for gold? One word.",
  "How many continents are there? Number only.",
  "What is 9 squared? Number only.",
  "What language is spoken in Brazil? Two words max.",
  "How many minutes in an hour? Number only.",
  "What is the largest ocean? Two words max.",
  "What is 100 divided by 4? Number only.",
  "Which instrument has 88 keys? One word.",
  "What is H2O commonly called? One word.",
  "How many legs does a spider have? Number only.",
  "What is the fastest land animal? Two words max.",
  "What is 12 times 12? Number only.",
  "Which country invented paper? One word.",
  "What is the currency of the UK? One word.",
  "How many seconds in a minute? Number only.",
  "What color does blue plus yellow make? One word.",
  "Boiling point of water in Celsius? Number only.",
  "Which ocean is off California's coast? Two words max.",
  "What is 7 plus 8 times 2? Number only.",
 ]),
 ("json-extract", "json", [
  'Extract as JSON with keys name and age: "Maria is 34 years old." Reply with only the JSON.',
  'Extract as JSON with keys city and country: "Lena lives in Oslo, Norway." Only JSON.',
  'Extract as JSON with keys product and price: "The widget costs 19.99 dollars." Only JSON.',
  'Extract as JSON with keys title and author: "Dune was written by Frank Herbert." Only JSON.',
  'Extract as JSON with keys event and year: "The moon landing happened in 1969." Only JSON.',
  'Extract as JSON with keys animal and legs: "A spider has 8 legs." Only JSON.',
  'Extract as JSON with keys company and founded: "Tesla was founded in 2003." Only JSON.',
  'Extract as JSON with keys fruit and color: "The banana is yellow." Only JSON.',
  'Extract as JSON with keys language and speakers: "Spanish has 500 million speakers." Only JSON.',
  'Extract as JSON with keys mountain and height_m: "Everest is 8848 meters tall." Only JSON.',
  'Extract as JSON with keys team and wins: "The Aces have 34 wins." Only JSON.',
  'Extract as JSON with keys book and pages: "It is 412 pages long." Only JSON.',
  'Extract as JSON with keys car and mpg: "The Civic gets 36 mpg." Only JSON.',
  'Extract as JSON with keys song and artist: "Halo is by Beyonce." Only JSON.',
  'Extract as JSON with keys river and length_km: "The Nile is 6650 km long." Only JSON.',
  'Extract as JSON with keys planet and moons: "Mars has 2 moons." Only JSON.',
  'Extract as JSON with keys school and students: "Roosevelt has 1200 students." Only JSON.',
  'Extract as JSON with keys phone and price_usd: "The Pixel costs 699 USD." Only JSON.',
  'Extract as JSON with keys dish and cuisine: "Sushi is Japanese." Only JSON.',
  'Extract as JSON with keys tool and use: "A hammer drives nails." Only JSON.',
 ]),
 ("classify", "basic", [
  "Classify as SPAM or NOT SPAM, one word: 'You won a free cruise, click now!'",
  "Classify as SPAM or NOT SPAM, one word: 'Meeting moved to 3pm, see you there.'",
  "Classify sentiment POSITIVE, NEGATIVE, or NEUTRAL, one word: 'I love this product!'",
  "Classify sentiment POSITIVE, NEGATIVE, or NEUTRAL, one word: 'This is the worst day ever.'",
  "Classify sentiment POSITIVE, NEGATIVE, or NEUTRAL, one word: 'The package arrived on Tuesday.'",
  "Classify as URGENT or NOT URGENT, one word: 'Server is down, customers affected.'",
  "Classify as URGENT or NOT URGENT, one word: 'Lunch menu for next week is posted.'",
  "Classify as SPAM or NOT SPAM, one word: 'Limited offer! Act now before midnight!'",
  "Classify sentiment POSITIVE, NEGATIVE, or NEUTRAL, one word: 'Meh, it was fine I guess.'",
  "Classify as QUESTION or STATEMENT, one word: 'What time does it close?'",
  "Classify as QUESTION or STATEMENT, one word: 'It closes at nine.'",
  "Classify as SPAM or NOT SPAM, one word: 'Your invoice for March is attached.'",
  "Classify sentiment POSITIVE, NEGATIVE, or NEUTRAL, one word: 'Absolutely fantastic experience!'",
  "Classify as URGENT or NOT URGENT, one word: 'Reminder: dentist appointment tomorrow.'",
  "Classify as FORMAL or CASUAL, one word: 'Hey dude, whats up?'",
  "Classify as FORMAL or CASUAL, one word: 'Dear Sir, I am writing to inquire.'",
  "Classify as SPAM or NOT SPAM, one word: 'Earn $5000 a week from home, no experience!'",
  "Classify sentiment POSITIVE, NEGATIVE, or NEUTRAL, one word: 'The food was cold and late.'",
  "Classify as QUESTION or STATEMENT, one word: 'Please send the report.'",
  "Classify as URGENT or NOT URGENT, one word: 'Fire alarm going off on floor 2.'",
 ]),
 ("summarize", "basic", [
  "Summarize in one sentence: 'The cat sat on the mat. It was a sunny day. The cat purred loudly and fell asleep.'",
  "Summarize in one sentence: 'Rain fell all morning. The streets flooded. By noon the sun came out and everything dried.'",
  "Summarize in one sentence: 'She opened the old book. Dust rose from its pages. Inside was a map she had never seen.'",
  "Summarize in one sentence: 'The team lost the first half. At halftime the coach changed tactics. They scored three goals and won.'",
  "Summarize in one sentence: 'He saved money for years. Finally he bought the small house by the lake. It needed repairs but he loved it.'",
  "Summarize in one sentence: 'The phone rang at midnight. It was her brother calling from abroad. They talked for an hour.'",
  "Summarize in one sentence: 'Seeds were planted in spring. Summer brought heavy rain. The autumn harvest was the best in years.'",
  "Summarize in one sentence: 'The software had a bug. Engineers worked through the night. A patch shipped by morning.'",
  "Summarize in one sentence: 'She missed her train. She caught a bus instead. She arrived only ten minutes late.'",
  "Summarize in one sentence: 'The museum opened a new wing. Thousands visited on the first day. The dinosaur exhibit was the favorite.'",
  "Summarize in one sentence: 'He trained every morning. The race day was cold and wet. He finished third, his best result yet.'",
  "Summarize in one sentence: 'The bakery ran out of flour. The owner drove to the mill herself. Fresh bread was ready by noon.'",
  "Summarize in one sentence: 'They hiked up the mountain. Fog hid the view at the top. On the way down the sky cleared.'",
  "Summarize in one sentence: 'The class studied hard. The exam was easier than expected. Everyone passed with good grades.'",
  "Summarize in one sentence: 'A storm knocked out power. Neighbors shared candles and food. The lights returned the next evening.'",
 ]),
 ("code-explain", "basic", [
  "In one sentence, what does this Python do: sorted(d.items())",
  "In one sentence, what does this Python do: [x*2 for x in range(5)]",
  "In one sentence, what does this Python do: 'hello'.upper()",
  "In one sentence, what does this Python do: len(set([1,2,2,3]))",
  "In one sentence, what does this Python do: {'a': 1}.get('b', 0)",
  "In one sentence, what does this Python do: open('f.txt').read().strip()",
  "In one sentence, what does this Python do: sum(range(101))",
  "In one sentence, what does this Python do: s[::-1]",
  "In one sentence, what does this Python do: max(d, key=d.get)",
  "In one sentence, what does this Python do: list(zip([1,2],[3,4]))",
  "In one sentence, what does this Python do: x = 5 if a else 10",
  "In one sentence, what does this Python do: ','.join(['a','b'])",
  "In one sentence, what does this Python do: isinstance(x, int)",
  "In one sentence, what does this Python do: round(3.14159, 2)",
  "In one sentence, what does this Python do: any([False, True])",
 ]),
 ("transform", "basic", [
  "Rewrite in a formal tone, one sentence: 'Hey, can you send me that file asap?'",
  "Rewrite as 3 bullet points: 'Buy milk, eggs, and bread. Then go to the bank. Call mom after.'",
  "Shorten to under 10 words: 'The reason for the delay was that the shipment arrived late due to weather.'",
  "Rewrite in ALL CAPS: 'quiet please'",
  "Rewrite as a question: 'You should drink more water.'",
  "Rewrite in a friendly tone, one sentence: 'Your request has been denied.'",
  "Convert to a numbered list: 'First wake up. Then brush teeth. Then eat breakfast.'",
  "Rewrite without the word 'very': 'The movie was very good and very exciting.'",
  "Rewrite as a haiku (5-7-5): 'morning coffee'",
  "Expand this abbreviation: 'ASAP' — reply with the full phrase only.",
  "Rewrite in past tense: 'She walks to work every day.'",
  "Rewrite as one tweet (under 280 chars): 'Our new product launches next Monday and we are very excited to share it with everyone who has been waiting patiently.'",
  "Rewrite politely, one sentence: 'Give me the report now.'",
  "Reverse the word order: 'the quick brown fox'",
  "Rewrite as a headline: 'Scientists discover a new species of frog in the Amazon rainforest.'",
 ]),
 ("reasoning", "basic", [
  "If all bloops are razzies and all razzies are lazzies, are all bloops lazzies? One word.",
  "A bat and ball cost $1.10. The bat costs $1 more than the ball. How much is the ball in cents? Number only.",
  "What comes next: 2, 4, 8, 16? Number only.",
  "If yesterday was Wednesday, what day is tomorrow? One word.",
  "Three boxes labeled apples, oranges, mixed are all mislabeled. You pick one fruit from one box. What's the minimum picks to fix all labels? Number only.",
  "A train goes 60 mph for 2.5 hours. How many miles? Number only.",
  "If some A are B and no B are C, can some A be C? Answer yes, no, or maybe.",
  "What is the next prime after 13? Number only.",
  "You have 5 pairs of socks in a drawer, how many must you take to guarantee a matching pair? Number only.",
  "If it takes 5 machines 5 minutes to make 5 widgets, how long for 100 machines to make 100 widgets in minutes? Number only.",
  "What comes next: 1, 1, 2, 3, 5, 8? Number only.",
  "A farmer has 17 sheep, all but 9 run away. How many remain? Number only.",
  "If two's company and three's a crowd, what are four and five? Two words.",
  "What is half of a quarter of 800? Number only.",
  "Mary's father has 5 daughters: Nana, Nene, Nini, Nono. What's the fifth daughter's name? One word.",
  "How many months have 28 days? Number only.",
  "What weighs more: a pound of feathers or a pound of bricks? One word: feathers, bricks, or same.",
  "If you rearrange CIFAIPC you get the name of a what? One word.",
  "What is 3 to the power of 4? Number only.",
  "A rooster lays an egg on a roof peak. Which side does it roll down? One sentence.",
 ]),
 ("entity-extract", "basic", [
  "List the cities mentioned, comma-separated: 'She flew from Paris to Tokyo, then visited Rome.'",
  "List the names mentioned, comma-separated: 'John met Lisa and they called Dr. Smith.'",
  "List the dates mentioned, comma-separated: 'Born on May 5 1990, married June 12 2015.'",
  "List the animals mentioned, comma-separated: 'The zoo has lions, tigers, and one bear.'",
  "List the colors mentioned, comma-separated: 'He wore a red shirt and blue jeans.'",
  "List the countries mentioned, comma-separated: 'Trade between Canada, Mexico, and Japan grew.'",
  "List the numbers mentioned, comma-separated: 'Order 42 items, 7 are fragile, total 99.'",
  "List the foods mentioned, comma-separated: 'Pizza, sushi, and tacos were served.'",
  "List the companies mentioned, comma-separated: 'Apple and Samsung compete with Google.'",
  "List the sports mentioned, comma-separated: 'He plays tennis but prefers swimming.'",
  "List the days mentioned, comma-separated: 'Open Monday through Friday, closed Sunday.'",
  "List the instruments mentioned, comma-separated: 'She plays piano and violin.'",
  "List the planets mentioned, comma-separated: 'Mars and Venus are visible, unlike Pluto.'",
  "List the languages mentioned, comma-separated: 'The sign was in English, French, and Arabic.'",
  "List the books mentioned, comma-separated: 'He read Dune and then Foundation.'",
 ]),
 ("compare", "basic", [
  "Which is better for a quick lunch: a sandwich or a three-course meal? One sentence why.",
  "Which is cheaper to run: a bicycle or a car? One sentence why.",
  "Which is faster: light or sound? One sentence.",
  "For learning Python, which is better: a book or interactive exercises? One sentence why.",
  "Which holds more: a teaspoon or a tablespoon? One sentence.",
  "For a password, which is stronger: 'password123' or 'Tr7#kQ!9z'? One sentence why.",
  "Which is heavier: a liter of water or a liter of oil? One sentence.",
  "For backups, which is safer: one copy or three copies? One sentence why.",
  "Which is more formal: 'Hi' or 'Dear Sir'? One sentence.",
  "Electric or gas stove: which heats faster? One sentence.",
  "For a long flight, which is better: aisle or window seat? One sentence why.",
  "Which is older: the pyramids or the Colosseum? One sentence.",
  "For watering plants, which is better: morning or noon? One sentence why.",
  "Which is brighter: the sun or the moon? One sentence.",
  "Pen or pencil for a permanent signature? One sentence why.",
 ]),
 ("generate", "basic", [
  "Write a one-line tagline for a coffee shop called 'The Daily Grind'.",
  "Write a haiku about rain.",
  "Write a one-sentence motivational quote about persistence.",
  "Invent a name for a pet robot dog. One name only.",
  "Write a two-word slogan for a gym.",
  "Write a one-line warning for a hot cup.",
  "Write a limerick first line about a coder named Lou.",
  "Write a one-sentence fortune cookie message.",
  "Invent a flavor name for a new ice cream. Three words max.",
  "Write a one-line epitaph for a retired keyboard.",
  "Write a three-word horror story.",
  "Write a one-line ad for invisible ink.",
  "Write a haiku about debugging.",
  "Invent a title for a cookbook about potatoes. Five words max.",
  "Write a one-sentence toast for a wedding.",
 ]),
]

def main():
    _, base_usage = key_usage()
    print("baseline key usage: $%.4f" % base_usage, flush=True)
    total_start = base_usage
    grand = {"attempted": 0, "ok": 0, "cost_reported": 0.0}
    for ei, (domain, check, prompts) in enumerate(EFFORTS, 1):
        _, u0 = key_usage()
        ok = 0
        models = {}
        cost_sum = 0.0
        lat = []
        findings = []
        for pi, p in enumerate(prompts):
            _, u_before = key_usage()
            if u_before - total_start > TOTAL_GUARD:
                print("TOTAL BUDGET GUARD: stopping everything", flush=True)
                return
            if (u_before - u0) > EFFORT_CAP:
                findings.append("per-effort cap reached; stopped early")
                break
            res = run_one(p, check)
            rec = {"effort": ei, "domain": domain, "idx": pi, "prompt": p, **res}
            with open(RESULTS, "a") as f:
                f.write(json.dumps(rec) + "\n")
            grand["attempted"] += 1
            if res["ok"]:
                ok += 1
                grand["ok"] += 1
            else:
                findings.append("FAIL idx %d: %s" % (pi, res.get("resp", "")[:120]))
            m = res.get("model") or "none"
            models[m] = models.get(m, 0) + 1
            cost_sum += res.get("cost", 0) or 0
            grand["cost_reported"] += res.get("cost", 0) or 0
            if res.get("latency"):
                lat.append(res["latency"])
        _, u1 = key_usage()
        spent = u1 - u0
        print("EFFORT %d/%d %-14s ok=%d/%d models=%s key_spent=$%.5f reported=$%.5f avg_lat=%.1fs" % (
            ei, len(EFFORTS), domain, ok, len(prompts), models, spent, cost_sum,
            sum(lat) / len(lat) if lat else 0), flush=True)
        if findings:
            for fl in findings[:6]:
                print("   note: " + fl, flush=True)
    _, u_end = key_usage()
    print("TOTAL key spend: $%.5f | reported sum: $%.5f | ok %d/%d" % (
        u_end - total_start, grand["cost_reported"], grand["ok"],
        grand["attempted"]), flush=True)

if __name__ == "__main__":
    main()
