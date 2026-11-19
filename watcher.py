#!/usr/bin/env python3
"""Forex plan watcher: reads plans.json, polls Twelve Data, sends Telegram alerts.
Alerts only. It never places trades. Standard library only."""
import json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("STATE_PATH", os.path.join(HERE, "state.json"))
TD_KEY = os.environ.get("TWELVEDATA_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
NEAR_PCT = 0.0030          # within 0.30% of the zone = poll every run
FAR_EVERY_MIN = 45         # otherwise poll this often
DAILY_CREDIT_CAP = 780     # free plan = 800/day
SIDEWAYS_H = {"A": 4, "B": 4, "C": 4}
REMIND_EVERY_MIN = 10
MAX_REMINDERS = 3
TD_SYMBOL = lambda s: s[:3] + "/" + s[3:]

def now_utc(): return datetime.now(timezone.utc)

def market_open(t):
    wd, h = t.weekday(), t.hour   # Mon=0
    if wd == 5: return False
    if wd == 6 and h < 22: return False
    if wd == 4 and h >= 22: return False
    return True

def load(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

def tg(text):
    if not (TG_TOKEN and TG_CHAT):
        print("[no telegram configured]", text); return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    try:
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data, timeout=20).read()
    except Exception as e:
        print("telegram error", e)

def fetch(symbols):
    """Return {symbol: [candles oldest->newest]} 5m candles (utc start times)."""
    out = {}
    if not symbols: return out
    q = urllib.parse.urlencode({"symbol": ",".join(TD_SYMBOL(s) for s in symbols), "interval": "5min",
                                "outputsize": 120, "timezone": "UTC", "apikey": TD_KEY})
    raw = json.loads(urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q, timeout=30).read())
    if len(symbols) == 1 and "values" in raw: raw = {TD_SYMBOL(symbols[0]): raw}
    for s in symbols:
        blk = raw.get(TD_SYMBOL(s), {})
        vals = blk.get("values")
        if not vals:
            print("no data for", s, blk.get("message", "")); continue
        c = []
        for v in reversed(vals):
            t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            c.append({"t": t, "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])})
        out[s] = c
    return out

def completed(candles, minutes, t):
    return [c for c in candles if c["t"] + timedelta(minutes=minutes) <= t]

def resample(candles5, minutes, t):
    """Group completed 5m candles into minutes-long candles; only fully formed groups."""
    groups = {}
    for c in candles5:
        k = int(c["t"].timestamp() // (minutes * 60))
        groups.setdefault(k, []).append(c)
    res = []
    need = minutes // 5
    for k in sorted(groups):
        g = groups[k]
        start = datetime.fromtimestamp(k * minutes * 60, timezone.utc)
        if len(g) == need and start + timedelta(minutes=minutes) <= t:
            res.append({"t": start, "o": g[0]["o"], "h": max(x["h"] for x in g),
                        "l": min(x["l"] for x in g), "c": g[-1]["c"]})
    return res

def fmt(p, x): return ("%." + str(p["dec"]) + "f") % x

def eval_plan(p, candles5, st, t):
    """Update st in place. Returns list of (kind, message)."""
    msgs = []
    sell = p["side"] == "sell"
    c5 = completed(candles5, 5, t)
    if not c5: return msgs
    last = c5[-1]["c"]
    st["last_price"] = last
    if st.get("stage") in ("cancelled", "done"): return msgs
    since = datetime.fromisoformat(st["armed_at"])
    c5 = [c for c in c5 if c["t"] >= since]
    if not c5: return msgs
    side = "SELL" if sell else "BUY"
    head = "%s %s (Strategy %s)" % (p["symbol"], side, p["strategy"])
    plan_line = "Entry %s | Stop %s | Target %s" % (fmt(p, p["entry"]), fmt(p, p["stop"]), fmt(p, p["target"]))

    # cancel: 1H close beyond the stop
    for c in resample(c5, 60, t):
        if (sell and c["c"] > p["stop"]) or (not sell and c["c"] < p["stop"]):
            st["stage"] = "cancelled"
            return [("cancel", "CANCELLED %s\nA 1H candle closed beyond the stop %s. Skip this plan." % (head, fmt(p, p["stop"])))]
    # missed: target reached before the zone was touched
    if st.get("stage") == "idle":
        for c in c5:
            if (sell and c["l"] <= p["target"]) or (not sell and c["h"] >= p["target"]):
                if not touched(p, c5, sell):
                    st["stage"] = "cancelled"
                    return [("cancel", "MISSED %s\nPrice reached the target %s without pulling back into the zone. Do not chase." % (head, fmt(p, p["target"])))]
                break
    # touch
    if st.get("stage") == "idle":
        tc = next((c for c in c5 if touched_one(p, c, sell)), None)
        if tc:
            st["stage"] = "zone"; st["touched_at"] = tc["t"].isoformat(); st["reminders"] = 0
            msgs.append(("zone", "ZONE TOUCHED %s\nPrice entered the zone %s-%s (now %s).\n%s\nWait for the confirmation before entering."
                         % (head, fmt(p, p["zone_lo"]), fmt(p, p["zone_hi"]), fmt(p, last), plan_line)))
    if st.get("stage") == "zone":
        tt = datetime.fromisoformat(st["touched_at"])
        after = [c for c in c5 if c["t"] >= tt]
        conf = confirm(p, after, sell, t)
        if conf:
            ratio = abs(p["target"] - conf) / max(abs(p["stop"] - conf), 1e-9)
            st["stage"] = "confirmed"; st["confirmed_at"] = t.isoformat(); st["reminders"] = 0; st["last_remind"] = t.isoformat()
            warn = "" if ratio >= 2 else "\nWARNING: real ratio from here is only 1:%.1f (below your 1:2 rule)." % ratio
            msgs.append(("confirm", "ENTER WINDOW %s\nConfirmation printed. Price now %s.\n%s\nRatio from the current price: 1:%.1f%s\nDecision and order are yours."
                         % (head, fmt(p, last), plan_line, ratio, warn)))
        elif t - tt > timedelta(hours=SIDEWAYS_H[p["strategy"]]):
            st["stage"] = "cancelled"
            msgs.append(("cancel", "SKIP %s\nSideways in the zone for %dh+ with no confirmation." % (head, SIDEWAYS_H[p["strategy"]])))
    elif st.get("stage") == "confirmed":
        lr = datetime.fromisoformat(st.get("last_remind", st["confirmed_at"]))
        if st.get("reminders", 0) < MAX_REMINDERS and t - lr >= timedelta(minutes=REMIND_EVERY_MIN):
            st["reminders"] = st.get("reminders", 0) + 1; st["last_remind"] = t.isoformat()
            msgs.append(("remind", "REMINDER %s still in the enter window. Price %s.\n%s" % (head, fmt(p, last), plan_line)))
        if t - datetime.fromisoformat(st["confirmed_at"]) > timedelta(hours=2): st["stage"] = "done"
    return msgs

def touched_one(p, c, sell): return c["h"] >= p["zone_lo"] if sell else c["l"] <= p["zone_hi"]
def touched(p, c5, sell): return any(touched_one(p, c, sell) for c in c5)

def confirm(p, after, sell, t):
    """Return confirming close price or None. A/C: 30m close beyond entry. B: 5m CHoCH after the zone high/low."""
    if not after: return None
    if p["strategy"] in ("A", "C"):
        for c in resample(after, 30, t):
            if (sell and c["c"] < p["entry"]) or (not sell and c["c"] > p["entry"]):
                return c["c"]
        return None
    # B: find the extreme since touch, then the swing before it, confirm on a 5m close through it
    ext_i = max(range(len(after)), key=lambda i: after[i]["h"]) if sell else min(range(len(after)), key=lambda i: after[i]["l"])
    lo = max(0, ext_i - 12)
    pre = after[lo:ext_i] or after[:ext_i + 1]
    if not pre: return None
    swing = min(c["l"] for c in pre) if sell else max(c["h"] for c in pre)
    for c in after[ext_i + 1:]:
        if (sell and c["c"] < swing) or (not sell and c["c"] > swing):
            return c["c"]
    return None

def main():
    t = now_utc()
    plans_doc = load(os.path.join(HERE, "plans.json"), {"plans": [], "rev": "0"})
    plans = plans_doc["plans"]; rev = plans_doc.get("rev", "0")
    state = load(STATE_PATH, {})
    if state.get("rev") != rev:
        state = {"rev": rev, "plans": {}, "fetch": {}, "credits": {}}
    state.setdefault("plans", {}); state.setdefault("fetch", {}); state.setdefault("credits", {})
    if not market_open(t):
        print("market closed"); save(state); return
    today = t.strftime("%Y-%m-%d")
    used = state["credits"].get(today, 0)
    to_fetch = []
    for p in plans:
        ps = state["plans"].setdefault(p["id"], {"stage": "idle", "armed_at": (t - timedelta(hours=1)).isoformat()})
        if ps["stage"] in ("cancelled", "done"): continue
        lp = ps.get("last_price")
        near = ps["stage"] != "idle" or lp is None or \
            min(abs(lp - p["zone_lo"]), abs(lp - p["zone_hi"])) / lp <= NEAR_PCT
        lf = state["fetch"].get(p["symbol"])
        due = lf is None or (t - datetime.fromisoformat(lf)) >= timedelta(minutes=FAR_EVERY_MIN)
        if (near or due) and p["symbol"] not in to_fetch: to_fetch.append(p["symbol"])
    if used + len(to_fetch) > DAILY_CREDIT_CAP:
        print("daily credit cap reached"); to_fetch = []
    data = {}
    if to_fetch:
        for i in range(0, len(to_fetch), 8):
            try: data.update(fetch(to_fetch[i:i + 8]))
            except Exception as e: print("fetch error", e)
        state["credits"] = {today: used + len(to_fetch)}
        for s in data: state["fetch"][s] = t.isoformat()
    for p in plans:
        ps = state["plans"][p["id"]]
        if p["symbol"] in data:
            for kind, m in eval_plan(p, data[p["symbol"]], ps, t):
                tg(m); print(m)
    save(state)

def save(state):
    with open(STATE_PATH, "w") as f: json.dump(state, f)

if __name__ == "__main__":
    main()
