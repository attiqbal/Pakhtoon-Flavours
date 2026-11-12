"""Learning Q&A memory.

The site remembers what customers ask and what the owner answers. When a new question is clearly the same as one she
has already answered, the saved answer goes out straight away and the owner is not bothered. Everything else is
handled as before (the AI assistant, or the owner).

How it stays safe
- Only answers the owner wrote herself, or approved, are ever reused. The AI's own answers are saved as *suggestions*
  that wait for her tick.
- Anything that depends on live facts (prices, opening hours, stock, order status, delivery or payment settings) is never
  reused. Those questions go to the assistant, which reads the live database through its tools.
- A reused answer needs a close match, an unambiguous winner and enough confidence. If she replies differently to a
  question the site answered, the saved answer loses confidence and stops being used until she has looked at it.
- Customer details (phone numbers, emails, postcodes, links, order numbers) are removed before a question is stored.

Matching is local and conservative: word overlap plus letter-trigram overlap, with hard guards for numbers and negations.
Questions that are only a partial match are optionally shown to the connected AI (the Claude/ChatGPT switch) with a strict
"SAME or DIFFERENT" check; that check can only confirm a saved answer the owner already approved, never write one.
When in doubt, nothing is answered automatically."""
import logging
import math
import re

from . import shop
from .db import execute, q, q1

log = logging.getLogger(__name__)

AUTO_SCORE = 0.85      # how close a new question must be to a saved one to answer straight away
BAND_SCORE = 0.45      # worth asking the AI whether it is the same question (only when an AI is connected)
MERGE_SCORE = 0.72     # close enough to count as the same question when saving or counting
MIN_CONFIDENCE = 0.5
MAX_QUESTION = 240     # long, multi-part messages are left to the assistant

STATUSES = ("approved", "suggested", "unanswered", "disabled")

STOP = set("""a an the is are was were be am do does did you your yours i me my we our us it its this that these those to of in on at
for with and or but so if then than can could would will shall should may might have has had get got there here any some please pls hi hello
hey hiya thanks thank thx ok okay yes yeah yep just also about as from by up out too very really what im ive id use make""".split())
NEGATIONS = {"no", "not", "without", "dont", "cant", "never", "free", "non", "cannot"}
SYNONYMS = {"veggie": "vegetarian", "veg": "vegetarian", "vegie": "vegetarian", "meat": "meat", "nuts": "nut", "peanuts": "peanut"}
QUESTION_WORDS = {"do", "does", "is", "are", "can", "could", "will", "would", "what", "when", "where", "which", "who", "how", "why", "any", "have", "has", "should"}

# Questions and answers that depend on things that change. These are never reused; the assistant looks them up live.
LIVE_QUESTION = re.compile(
    r"\b(price|prices|priced|cost|costs|how much|£|open|opens|opening|hours|close|closes|closed|closing|today|tomorrow|tonight|"
    r"this week|next week|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday|stock|available|availability|sold out|"
    r"my order|order status|order number|track|tracking|ready|late|delay|delayed|where is|where's|deliver|delivery|delivering|"
    r"collection|collect|pick up|pickup|pay|paid|payment|card|cash|bank|transfer|minimum|charge|fee|total|refund|cancel|cancelled|cancellation|"
    r"discount|offer|offers|deal|special|menu today|tiffin)\b", re.I)
LIVE_ANSWER = re.compile(
    r"(£|\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s?(am|pm)\b|\b(price|prices|priced|cost|costs|open|opens|opening|hours|closed|closing|today|tomorrow|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|in stock|out of stock|sold out|available|availability|your order|order (no|number)|"
    r"order #|paid|payment received|discount|offer)\b|#\d+)", re.I)

PII = [(re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
       (re.compile(r"https?://\S+|www\.\S+"), "[link]"),
       (re.compile(r"\b[A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2}\b"), "[postcode]"),
       (re.compile(r"\+?\d[\d\s-]{6,}\d"), "[phone]"),
       (re.compile(r"#\s?\d+|\border\s*(no\.?|number)?\s*\d+\b", re.I), "[order]")]


# ---------------------------------------------------------------- text handling

def redact(text: str) -> str:
    text = (text or "").strip()
    for rx, token in PII:
        text = rx.sub(token, text)
    return re.sub(r"\s+", " ", text)[:600]


def _stem(w: str) -> str:
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith(("shes", "ches", "xes", "sses")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str) -> list[str]:
    text = re.sub(r"\[(email|link|postcode|phone|order)\]", " ", (text or "").lower().replace("'", "").replace("’", ""))
    out = []
    for w in re.findall(r"[a-z0-9£]+", text):
        if w in STOP:
            continue
        w = _stem(w)
        out.append(SYNONYMS.get(w, w))
    return out


def _trigrams(toks: list[str]) -> set[str]:
    s = " ".join(sorted(set(toks)))
    return {s[i:i + 3] for i in range(max(len(s) - 2, 1))} if s else set()


def similarity(a: list[str], b: list[str]) -> float:
    """0..1. Word overlap and spelling overlap, with hard guards: numbers and negations must agree
    ('with nuts' is not 'without nuts', '2 people' is not '4 people')."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    if {t for t in sa if t.isdigit()} != {t for t in sb if t.isdigit()} or (sa & NEGATIONS) != (sb & NEGATIONS):
        return 0.0
    cos = len(sa & sb) / math.sqrt(len(sa) * len(sb))
    ta, tb = _trigrams(a), _trigrams(b)
    dice = 2 * len(ta & tb) / (len(ta) + len(tb)) if ta and tb else 0.0
    return round(0.65 * cos + 0.35 * dice, 4)


def is_live_question(text: str) -> bool:
    return bool(LIVE_QUESTION.search(text or ""))


def answer_block(answer: str, customer_name: str = "") -> str:
    """Why an answer must not be reused, or '' if it can be. Names/numbers belong to one customer; the rest changes."""
    if LIVE_ANSWER.search(answer or ""):
        return "live"
    for part in re.findall(r"[A-Za-z]{3,}", customer_name or ""):
        if re.search(rf"\b{re.escape(part)}\b", answer, re.I):
            return "personal"
    if re.search(r"\d{5,}", answer or ""):
        return "personal"
    return ""


def question_like(text: str) -> bool:
    t = (text or "").strip().lower()
    return "?" in t or (t.split()[:1] and t.split()[0] in QUESTION_WORDS)


def confidence(e: dict) -> float:
    """1.0 for a clean record. Each time the owner replied differently to this answer, it counts for less;
    every time it was used without complaint, it recovers."""
    return (e["hits"] + 3) / (e["hits"] + 3 + 2 * e["corrections"])


# ---------------------------------------------------------------- storage

def _entries(statuses: tuple = STATUSES) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    rows = q(f"SELECT * FROM qa_entries WHERE status IN ({marks})", statuses)
    phr = {}
    for p in q("SELECT entry_id, text FROM qa_phrasings"):
        phr.setdefault(p["entry_id"], []).append(p["text"])
    for e in rows:
        e["phrasings"] = phr.get(e["id"], [e["question"]])
    return rows


def _score(toks: list[str], e: dict) -> float:
    return max((similarity(toks, tokens(p)) for p in e["phrasings"]), default=0.0)


def _closest(text: str, statuses: tuple = STATUSES) -> tuple[dict | None, float]:
    toks = tokens(text)
    best, best_s = None, 0.0
    for e in _entries(statuses):
        s = _score(toks, e)
        if s > best_s:
            best, best_s = e, s
    return best, best_s


def _add_phrasing(entry_id: int, question: str) -> None:
    if not question:
        return
    norm = " ".join(sorted(set(tokens(question))))
    if not norm:
        return
    if not q1("SELECT 1 FROM qa_phrasings WHERE entry_id=? AND norm=?", (entry_id, norm)):
        execute("INSERT INTO qa_phrasings(entry_id,text,norm) VALUES(?,?,?)", (entry_id, question, norm))
        n = q1("SELECT COUNT(*) n FROM qa_phrasings WHERE entry_id=?", (entry_id,))["n"]
        if n > 12:       # keep the few most recent wordings
            execute("DELETE FROM qa_phrasings WHERE id IN (SELECT id FROM qa_phrasings WHERE entry_id=? ORDER BY id LIMIT ?)", (entry_id, n - 12))


def _create(question: str, answer: str, source: str, status: str, blocked: str = "") -> int:
    now = shop.iso_now()
    eid = execute("INSERT INTO qa_entries(question,answer,source,status,blocked,asks,last_asked,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
                  (question, answer, source, status, blocked, now, now, now))
    _add_phrasing(eid, question)
    return eid


def _touch(entry_id: int, question: str) -> None:
    execute("UPDATE qa_entries SET asks=asks+1, last_asked=?, updated_at=? WHERE id=?", (shop.iso_now(), shop.iso_now(), entry_id))
    _add_phrasing(entry_id, question)


# ---------------------------------------------------------------- answering customers

def _usable(e: dict) -> bool:
    return not e["blocked"] and bool(e["answer"].strip()) and not e["pending_answer"] and confidence(e) >= MIN_CONFIDENCE


def find_answer(text: str, judge=None) -> dict | None:
    """A saved, approved answer that is clearly right for this question, or None (then the assistant or the owner answers).
    `judge(text, entry) -> bool` is an optional second opinion for near misses (see the module notes)."""
    text = (text or "").strip()
    if not shop.setting("qa_auto") or not text or len(text) > MAX_QUESTION or is_live_question(text):
        return None
    toks = tokens(text)
    if not toks:
        return None
    scored = sorted(((_score(toks, e), e) for e in _entries(("approved",))), key=lambda x: -x[0])
    if not scored:
        return None
    best_s, best = scored[0]
    floor = AUTO_SCORE if judge is None else BAND_SCORE
    if best_s < floor or not _usable(best):
        return None
    # two different saved answers about equally close: don't guess
    if any(s >= best_s - 0.03 and e["answer"].strip() != best["answer"].strip() for s, e in scored[1:] if _usable(e) and s >= floor):
        return None
    if best_s >= AUTO_SCORE:
        return {**best, "score": best_s, "judged": False}
    try:
        same = judge(text, best)
    except Exception:  # noqa: BLE001  (API down: simply don't auto-answer)
        log.exception("Q&A match check failed")
        return None
    return {**best, "score": best_s, "judged": True} if same else None


def mark_used(entry_id: int, question: str, learn_wording: bool = True) -> None:
    execute("UPDATE qa_entries SET hits=hits+1, asks=asks+1, last_used=?, last_asked=?, updated_at=? WHERE id=?",
            (shop.iso_now(), shop.iso_now(), shop.iso_now(), entry_id))
    if learn_wording:     # a wording only the AI vouched for is not remembered, so one wrong call can't stick
        _add_phrasing(entry_id, question)


def last_customer_line(conv_id: int, after_owner_only: bool = False) -> dict | None:
    """The customer's latest message. With after_owner_only, only one the owner has not replied to yet, so a second
    friendly message from her ("see you Friday!") is never mistaken for another answer."""
    senders = "('owner')" if after_owner_only else "('owner','ai')"
    last_reply = q1(f"SELECT COALESCE(MAX(id),0) m FROM chat_log WHERE conversation_id=? AND sender IN {senders}", (conv_id,))["m"]
    line = q1("SELECT * FROM chat_log WHERE conversation_id=? AND sender='customer' AND id>? ORDER BY id DESC LIMIT 1", (conv_id, last_reply))
    if line or after_owner_only:
        return line
    return q1("SELECT * FROM chat_log WHERE conversation_id=? AND sender='customer' ORDER BY id DESC LIMIT 1", (conv_id,))


# ---------------------------------------------------------------- learning

def learn_from_owner(conv_id: int, answer: str) -> None:
    """She just replied in a chat. Pair her words with the customer question they answer, then save or update it."""
    answer = (answer or "").strip()
    line = last_customer_line(conv_id, after_owner_only=True)
    if not answer or not line:
        return
    # which line is she answering? the customer's message just before her reply (the one we just logged is hers)
    question = redact(line["text"])
    if not tokens(question) or len(question) > MAX_QUESTION:
        return
    conv = q1("SELECT name FROM conversations WHERE id=?", (conv_id,)) or {}
    blocked = "live" if is_live_question(question) else answer_block(answer, conv.get("name") or "")
    # was this very question answered by a saved answer just now? then she is correcting it
    used = q1("SELECT qa_id FROM chat_log WHERE conversation_id=? AND qa_id IS NOT NULL AND id>? ORDER BY id DESC LIMIT 1", (conv_id, line["id"]))
    near, s = _closest(question)
    if near and s >= MERGE_SCORE:
        if used and used["qa_id"] == near["id"] and _answer_overlap(near["answer"], answer) < 0.6:
            execute("UPDATE qa_entries SET corrections=corrections+1, pending_answer=?, updated_at=? WHERE id=?", (answer, shop.iso_now(), near["id"]))
            shop.alert("question", f"You replied differently to a saved answer ({near['question'][:80]}). Check it under Answers.", "/owner/answers")
            return
        if used and used["qa_id"] == near["id"]:
            return       # she said the same thing again; nothing to learn
        _add_phrasing(near["id"], question)
        status = near["status"] if near["status"] == "disabled" else "approved"
        execute("UPDATE qa_entries SET answer=?, source='owner', status=?, blocked=?, pending_answer='', updated_at=? WHERE id=?",
                (answer, status, blocked, shop.iso_now(), near["id"]))
        return
    _create(question, answer, "owner", "approved", blocked)


def _answer_overlap(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    return len(ta & tb) / max(min(len(ta), len(tb)), 1) if ta and tb else 0.0


def note_unanswered(conv_id: int) -> None:
    """The assistant couldn't answer (or passed it on). Keep the question so it shows under 'Waiting for your answer'."""
    line = last_customer_line(conv_id)
    if not line:
        return
    question = redact(line["text"])
    if not tokens(question) or len(question) > MAX_QUESTION:
        return
    near, s = _closest(question)
    if near and s >= MERGE_SCORE:
        _touch(near["id"], question)
        return
    _create(question, "", "none", "unanswered", "live" if is_live_question(question) else "")


def note_ai_answer(conv_id: int, question_text: str, reply: str) -> None:
    """The assistant answered a plain question without looking anything up. Save it as a suggestion she can approve."""
    question = redact(question_text)
    if not tokens(question) or len(question) > MAX_QUESTION or not question_like(question_text) or not reply.strip():
        return
    conv = q1("SELECT name FROM conversations WHERE id=?", (conv_id,)) or {}
    near, s = _closest(question)
    if near and s >= MERGE_SCORE:
        _touch(near["id"], question)
        return
    blocked = "live" if is_live_question(question) else answer_block(reply, conv.get("name") or "")
    _create(question, reply.strip()[:1500], "ai", "suggested", blocked)


# ---------------------------------------------------------------- owner actions

def _need(entry_id: int) -> dict:
    e = q1("SELECT * FROM qa_entries WHERE id=?", (entry_id,))
    if not e:
        raise shop.ShopError("That saved answer no longer exists.")
    return e


def save(entry_id: int, question: str, answer: str, force: bool = False) -> dict:
    """Edit from the admin page. Her edit makes it hers: approved, and any pending correction is cleared."""
    e = _need(entry_id)
    question, answer = redact(question), (answer or "").strip()[:1500]
    if not tokens(question):
        raise shop.ShopError("Write the question the way a customer would ask it.")
    if not answer:
        raise shop.ShopError("Write the answer first.")
    blocked = "" if force else ("live" if is_live_question(question) else answer_block(answer))
    status = "disabled" if e["status"] == "disabled" else "approved"
    execute("UPDATE qa_entries SET question=?, answer=?, source='owner', status=?, blocked=?, pending_answer='', updated_at=? WHERE id=?",
            (question, answer, status, blocked, shop.iso_now(), entry_id))
    _add_phrasing(entry_id, question)
    return _need(entry_id)


def add(question: str, answer: str, force: bool = False) -> int:
    question, answer = redact(question), (answer or "").strip()[:1500]
    if not tokens(question):
        raise shop.ShopError("Write the question the way a customer would ask it.")
    if not answer:
        raise shop.ShopError("Write the answer first.")
    blocked = "" if force else ("live" if is_live_question(question) else answer_block(answer))
    eid = _create(question, answer, "owner", "approved", blocked)
    execute("UPDATE qa_entries SET asks=0 WHERE id=?", (eid,))
    return eid


def approve(entry_id: int) -> None:
    e = _need(entry_id)
    if not e["answer"].strip():
        raise shop.ShopError("Write an answer first.")
    execute("UPDATE qa_entries SET status='approved', updated_at=? WHERE id=?", (shop.iso_now(), entry_id))


def set_enabled(entry_id: int, on: bool) -> None:
    e = _need(entry_id)
    if on and not e["answer"].strip():
        raise shop.ShopError("Write an answer first.")
    execute("UPDATE qa_entries SET status=?, updated_at=? WHERE id=?",
            ("approved" if on else "disabled", shop.iso_now(), entry_id))


def delete(entry_id: int) -> None:
    _need(entry_id)
    execute("DELETE FROM qa_entries WHERE id=?", (entry_id,))


def accept_correction(entry_id: int, use_new: bool) -> None:
    e = _need(entry_id)
    if use_new and e["pending_answer"]:
        execute("UPDATE qa_entries SET answer=?, pending_answer='', blocked=?, updated_at=? WHERE id=?",
                (e["pending_answer"], answer_block(e["pending_answer"]), shop.iso_now(), entry_id))
    else:
        execute("UPDATE qa_entries SET pending_answer='', updated_at=? WHERE id=?", (shop.iso_now(), entry_id))


def merge(entry_id: int, into_id: int) -> None:
    """Two questions that are really one. Wordings and counts move to the target, which keeps its own answer."""
    if entry_id == into_id:
        raise shop.ShopError("Pick a different entry to merge into.")
    a, b = _need(entry_id), _need(into_id)
    for p in q("SELECT text FROM qa_phrasings WHERE entry_id=?", (a["id"],)):
        _add_phrasing(b["id"], p["text"])
    execute("UPDATE qa_entries SET asks=asks+?, hits=hits+?, corrections=corrections+?, last_asked=MAX(COALESCE(last_asked,''),?), "
            "last_used=MAX(COALESCE(last_used,''),?), updated_at=? WHERE id=?",
            (a["asks"], a["hits"], a["corrections"], a["last_asked"] or "", a["last_used"] or "", shop.iso_now(), b["id"]))
    execute("UPDATE chat_log SET qa_id=? WHERE qa_id=?", (b["id"], a["id"]))
    execute("DELETE FROM qa_entries WHERE id=?", (a["id"],))


def explain(text: str) -> dict:
    """For the 'try a question' box: what would happen if a customer asked this now."""
    text = (text or "").strip()
    if not text:
        return {}
    if is_live_question(text):
        return {"result": "assistant", "why": "It depends on live information (prices, hours, orders, delivery or payment), so the assistant looks it up each time."}
    if not shop.setting("qa_auto"):
        return {"result": "assistant", "why": "Automatic answers are switched off, so the assistant or you would answer."}
    near, s = _closest(text, ("approved", "suggested", "unanswered", "disabled"))
    hit = find_answer(text)
    if hit:
        return {"result": "answer", "entry": hit, "score": hit["score"], "why": "A close match to a saved answer, so it would be answered straight away."}
    if near and s >= MERGE_SCORE:
        why = {"approved": "Close, but not close enough or not trusted enough to answer without you.",
               "suggested": "Matches an answer waiting for your approval, so it is not used yet.",
               "unanswered": "Matches a question still waiting for your answer.",
               "disabled": "Matches an answer you switched off."}[near["status"]]
        return {"result": "assistant", "entry": near, "score": s, "why": why + " The assistant or you would answer."}
    return {"result": "assistant", "why": "Nothing saved is close to it, so the assistant or you would answer."}


def overview() -> dict:
    rows = q("SELECT * FROM qa_entries ORDER BY asks DESC, updated_at DESC")
    for r in rows:
        r["confidence"] = round(confidence(r), 2)
        r["phrasings"] = [p["text"] for p in q("SELECT text FROM qa_phrasings WHERE entry_id=? ORDER BY id DESC", (r["id"],))]
    by = lambda f: [r for r in rows if f(r)]  # noqa: E731
    stats = q1("SELECT COALESCE(SUM(hits),0) hits, COALESCE(SUM(corrections),0) corrections FROM qa_entries")
    return {
        "review": by(lambda r: r["pending_answer"]),
        "waiting": by(lambda r: r["status"] == "unanswered" and not r["blocked"]),
        "suggested": by(lambda r: r["status"] == "suggested" and not r["blocked"] and not r["pending_answer"]),
        "saved": by(lambda r: r["status"] in ("approved", "disabled") and not r["blocked"] and not r["pending_answer"]),
        "live": by(lambda r: r["blocked"]),
        "hits": stats["hits"], "corrections": stats["corrections"], "total": len(rows),
        "others": [{"id": r["id"], "question": r["question"]} for r in rows if r["answer"].strip()],
    }
