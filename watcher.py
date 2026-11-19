#!/usr/bin/env python3
"""Forex plan watcher v2: reads plans.json, polls Twelve Data, sends Telegram alerts,
tracks every signal to its result and keeps a journal.csv for later analysis.
Alerts only. It never places trades. Standard library only."""
import csv, io, json, os, uuid, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:
    PARIS = timezone(timedelta(hours=1))

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.environ.get("STATE_PATH", os.path.join(HERE, "state.json"))
JOURNAL_PATH = os.environ.get("JOURNAL_PATH", os.path.join(HERE, "journal.csv"))
PLANS_PATH = os.environ.get("PLANS_PATH", os.path.join(HERE, "plans.json"))
TD_KEY = os.environ.get("TWELVEDATA_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ENV = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NEAR_PCT = 0.0030           # within 0.30% of the zone = "near"
NEAR_EVERY_MIN = 10         # near but not yet in the zone
FAR_EVERY_MIN = 45          # far from the zone
TRACK_EVERY_MIN = 15        # open trades being tracked
DAILY_CREDIT_CAP = 780      # Twelve Data free plan = 800/day
SIDEWAYS_H = {"A": 4, "B": 4, "C": 4}
NOW_MIN, LAST_CALL_MIN = 5, 10   # place now (<=5 min after the signal candle closed), last call (<=10)
CHASE_FRAC = 0.25           # max chase = 25% of the risk beyond the signal price
TRACK_EXPIRE_H = 24
STALE_MIN = 25              # newest 5m candle older than this = ignore (market closed / feed stale)
TD_SYMBOL = lambda s: s[:3] + "/" + s[3:]
JOURNAL_COLS = ["trade_id", "symbol", "strategy", "side", "plan_id", "zone_touched_utc", "signal_utc",
                "signal_price", "planned_entry", "stop", "target", "ratio_at_signal", "alert_delay_min",
                "placed", "user_fill", "placed_utc", "resolved_utc", "result", "r_multiple", "mfe_r",
                "mae_r", "minutes_to_resolve", "weekday_paris", "hour_paris", "note"]


def now_utc(): return datetime.now(timezone.utc)
def iso(t): return t.isoformat()
def pt(t): return t.astimezone(PARIS).strftime("%H:%M")
def parse(s): return datetime.fromisoformat(s)
def fmt(p, x): return ("%." + str(p["dec"]) + "f") % x


def market_open(t):
    wd, h = t.weekday(), t.hour
    if wd == 5: return False
    if wd == 6 and h < 22: return False
    if wd == 4 and h >= 22: return False
    return True


def load(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default


def save(state):
    with open(STATE_PATH, "w") as f: json.dump(state, f)


# ---------- Telegram ----------
def tg_call(method, payload):
    if not TG_TOKEN: return None
    data = urllib.parse.urlencode(payload).encode()
    try:
        return json.loads(urllib.request.urlopen("https://api.telegram.org/bot%s/%s" % (TG_TOKEN, method), data, timeout=20).read())
    except Exception as e:
        print("telegram error", method, e); return None


def tg(state, text, buttons=None):
    chat = TG_CHAT_ENV or state.get("chat_id")
    print("[TG]", text.replace("\n", " / "))
    if not (TG_TOKEN and chat): return
    p = {"chat_id": chat, "text": text}
    if buttons: p["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})
    tg_call("sendMessage", p)


def tg_document(state, path, caption):
    chat = TG_CHAT_ENV or state.get("chat_id")
    if not (TG_TOKEN and chat and os.path.exists(path)): return
    b = uuid.uuid4().hex
    body = b""
    for k, v in (("chat_id", str(chat)), ("caption", caption)):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\nContent-Type: text/csv\r\n\r\n"
             % (b, os.path.basename(path))).encode() + open(path, "rb").read() + ("\r\n--%s--\r\n" % b).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendDocument" % TG_TOKEN, body,
                                 {"Content-Type": "multipart/form-data; boundary=" + b})
    try: urllib.request.urlopen(req, timeout=30).read()
    except Exception as e: print("telegram doc error", e)


# ---------- journal ----------
def journal_add(row):
    new = not os.path.exists(JOURNAL_PATH)
    with open(JOURNAL_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, JOURNAL_COLS, extrasaction="ignore")
        if new: w.writeheader()
        w.writerow(row)


def journal_rows():
    try:
        with open(JOURNAL_PATH, newline="") as f: return list(csv.DictReader(f))
    except Exception: return []


def summary_text(rows):
    done = [r for r in rows if r.get("result") in ("win", "loss", "expired")]
    if not done: return "Journal: no resolved trades yet."
    def stats(rs):
        w = sum(1 for r in rs if r["result"] == "win"); tot = sum(float(r["r_multiple"] or 0) for r in rs)
        return "%d trades, %d wins (%.0f%%), %+.1fR" % (len(rs), w, 100.0 * w / len(rs), tot)
    placed = [r for r in done if r.get("placed") == "yes"]
    lines = ["JOURNAL SUMMARY", "All signals: " + stats(done), "Placed by you: " + (stats(placed) if placed else "none logged")]
    for s in ("A", "B", "C"):
        rs = [r for r in done if r["strategy"] == s]
        if rs: lines.append("Strategy %s: %s" % (s, stats(rs)))
    skipped = len([r for r in rows if r.get("result") in ("missed", "cancelled", "skip")])
    lines.append("Plans that never triggered (missed/cancelled/skip): %d" % skipped)
    return "\n".join(lines)


# ---------- market data ----------
def fetch(symbols):
    out = {}
    if not symbols: return out
    q = urllib.parse.urlencode({"symbol": ",".join(TD_SYMBOL(s) for s in symbols), "interval": "5min",
                                "outputsize": 120, "timezone": "UTC", "apikey": TD_KEY})
    raw = json.loads(urllib.request.urlopen("https://api.twelvedata.com/time_series?" + q, timeout=30).read())
    if len(symbols) == 1 and ("values" in raw or "message" in raw): raw = {TD_SYMBOL(symbols[0]): raw}
    for s in symbols:
        blk = raw.get(TD_SYMBOL(s), {})
        vals = blk.get("values")
        if not vals:
            print("no data for", s, blk.get("message", "")); out[s] = None; continue
        c = []
        for v in reversed(vals):
            t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            c.append({"t": t, "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"])})
        out[s] = c
    return out


def completed(candles, minutes, t):
    return [c for c in candles if c["t"] + timedelta(minutes=minutes) <= t]


def resample(candles5, minutes, t):
    groups = {}
    for c in candles5:
        groups.setdefault(int(c["t"].timestamp() // (minutes * 60)), []).append(c)
    res, need = [], minutes // 5
    for k in sorted(groups):
        g = groups[k]
        start = datetime.fromtimestamp(k * minutes * 60, timezone.utc)
        if len(g) == need and start + timedelta(minutes=minutes) <= t:
            res.append({"t": start, "o": g[0]["o"], "h": max(x["h"] for x in g), "l": min(x["l"] for x in g), "c": g[-1]["c"]})
    return res

# ---------- plan logic ----------
def touched_one(p, c, sell): return c["h"] >= p["zone_lo"] if sell else c["l"] <= p["zone_hi"]
def touched(p, c5, sell): return any(touched_one(p, c, sell) for c in c5)


def confirm(p, after, sell, t):
    """Return (price, candle_close_time) of the confirming candle, or None."""
    if not after: return None
    if p["strategy"] in ("A", "C"):
        for c in resample(after, 30, t):
            if (sell and c["c"] < p["entry"]) or (not sell and c["c"] > p["entry"]):
                return c["c"], c["t"] + timedelta(minutes=30)
        return None
    ext_i = max(range(len(after)), key=lambda i: after[i]["h"]) if sell else min(range(len(after)), key=lambda i: after[i]["l"])
    pre = after[max(0, ext_i - 12):ext_i] or after[:ext_i + 1]
    if not pre: return None
    swing = min(c["l"] for c in pre) if sell else max(c["h"] for c in pre)
    for c in after[ext_i + 1:]:
        if (sell and c["c"] < swing) or (not sell and c["c"] > swing):
            return c["c"], c["t"] + timedelta(minutes=5)
    return None


def head_of(p):
    return "%s %s (Strategy %s)" % (p["symbol"], "SELL" if p["side"] == "sell" else "BUY", p["strategy"])


def log_plan_end(p, ps, kind, note, t):
    journal_add({"trade_id": p["id"] + "-" + t.strftime("%m%dT%H%M"), "symbol": p["symbol"], "strategy": p["strategy"],
                 "side": p["side"], "plan_id": p["id"], "zone_touched_utc": ps.get("touched_at", ""), "planned_entry": p["entry"],
                 "stop": p["stop"], "target": p["target"], "resolved_utc": iso(t), "result": kind, "note": note,
                 "weekday_paris": t.astimezone(PARIS).strftime("%a"), "hour_paris": t.astimezone(PARIS).hour})


def eval_plan(p, candles5, ps, t, state, trades):
    """Update plan state; new confirmed signals become trade records. Returns list of (kind, text, buttons)."""
    msgs = []
    sell = p["side"] == "sell"
    c5 = completed(candles5, 5, t)
    if not c5: return msgs
    ps["last_price"] = c5[-1]["c"]
    if ps.get("stage") in ("cancelled", "done"): return msgs
    c5 = [c for c in c5 if c["t"] >= parse(ps["armed_at"])]
    if not c5: return msgs
    last = c5[-1]["c"]
    head = head_of(p)
    plan_line = "Entry %s | Stop %s | Target %s" % (fmt(p, p["entry"]), fmt(p, p["stop"]), fmt(p, p["target"]))

    for c in resample(c5, 60, t):   # stop broken on a 1H close
        if (sell and c["c"] > p["stop"]) or (not sell and c["c"] < p["stop"]):
            ps["stage"] = "cancelled"
            log_plan_end(p, ps, "cancelled", "1H close beyond stop", t)
            return [("cancel", "CANCELLED %s\nA 1H candle closed beyond the stop %s. Skip this plan." % (head, fmt(p, p["stop"])), None)]
    if ps.get("stage") == "idle":   # target hit before any zone touch
        for c in c5:
            if (sell and c["l"] <= p["target"]) or (not sell and c["h"] >= p["target"]):
                if not touched(p, c5, sell):
                    ps["stage"] = "cancelled"
                    log_plan_end(p, ps, "missed", "target reached without zone touch", t)
                    return [("cancel", "MISSED %s\nPrice reached the target %s without pulling back into the zone. Do not chase." % (head, fmt(p, p["target"])), None)]
                break
    if ps.get("stage") == "idle":
        tc = next((c for c in c5 if touched_one(p, c, sell)), None)
        if tc:
            ps["stage"] = "zone"; ps["touched_at"] = iso(tc["t"])
            msgs.append(("zone", "GET READY - %s\nPrice entered the zone %s-%s (now %s). NOT an entry yet.\n%s\nI will message you the moment the confirmation prints, with the exact time window."
                         % (head, fmt(p, p["zone_lo"]), fmt(p, p["zone_hi"]), fmt(p, last), plan_line), None))
    if ps.get("stage") == "zone":
        tt = parse(ps["touched_at"])
        conf = confirm(p, [c for c in c5 if c["t"] >= tt], sell, t)
        if conf:
            price, closed = conf
            risk = abs(price - p["stop"])
            ratio = abs(p["target"] - price) / max(risk, 1e-9)
            tid = p["id"] + "-" + closed.strftime("%m%dT%H%M")
            trade = {"id": tid, "plan": p, "state": "window", "signal_price": price, "signal_close": iso(closed),
                     "touched_at": ps["touched_at"], "ratio": ratio, "placed": None, "fill": None, "placed_at": None,
                     "first_alert": iso(t), "scan_from": iso(closed), "best": price, "worst": price, "last_call": False}
            trades.append(trade)
            ps["stage"] = "done"
            age = (t - closed).total_seconds() / 60.0
            msgs.append(window_msg(trade, age, last, t, first=True))
        elif t - tt > timedelta(hours=SIDEWAYS_H[p["strategy"]]):
            ps["stage"] = "cancelled"
            log_plan_end(p, ps, "skip", "sideways in zone, no confirmation", t)
            msgs.append(("cancel", "SKIP %s\nSideways in the zone for %dh+ with no confirmation. Plan dropped." % (head, SIDEWAYS_H[p["strategy"]]), None))
    return msgs


def window_msg(tr, age, last, t, first=False):
    p = tr["plan"]; sell = p["side"] == "sell"; sp = tr["signal_price"]
    risk = abs(sp - p["stop"])
    chase = sp - CHASE_FRAC * risk if sell else sp + CHASE_FRAC * risk
    closed = parse(tr["signal_close"])
    until = closed + timedelta(minutes=LAST_CALL_MIN)
    warn = "" if tr["ratio"] >= 2 else "\nWARNING: ratio from here is only 1:%.1f (below your 1:2 rule) - consider skipping." % tr["ratio"]
    core = ("Confirmation candle closed %s Paris (%d min ago). Price now %s.\n%s %s at about %s. Do NOT enter %s %s (chasing).\n"
            "Stop %s | Target %s | Ratio 1:%.1f%s\nValid until %s Paris."
            % (pt(closed), int(age), fmt(p, last), "SELL" if sell else "BUY", p["symbol"], fmt(p, sp),
               "below" if sell else "above", fmt(p, chase), fmt(p, p["stop"]), fmt(p, p["target"]), tr["ratio"], warn, pt(until)))
    btn = [{"text": "Placed", "callback_data": "P:" + tr["id"]}, {"text": "Skipped", "callback_data": "S:" + tr["id"]}]
    if age <= NOW_MIN:
        return ("now", "PLACE THE TRADE NOW - %s\n%s\nAfter placing it, tap Placed (or send /fill %s PRICE with your real fill)."
                % (head_of(p), core, p["symbol"]), btn)
    if age <= LAST_CALL_MIN:
        tr["last_call"] = True
        return ("last", "LAST CALL - place within 5 minutes - %s\n%s" % (head_of(p), core), btn)
    tr["state"] = "tracking"; tr["placed"] = tr["placed"] if tr["placed"] is not None else False
    return ("closed", "WINDOW CLOSED - %s\nThe confirmation was %d min ago. Do not chase. I will still track it so the result is in your journal."
            % (head_of(p), int(age)), None)


def update_window(tr, last, t):
    """Called each run for trades in the 'window' state. Returns list of messages."""
    if tr["placed"] is not None:      # user answered: stop nagging
        tr["state"] = "tracking"; return []
    age = (t - parse(tr["signal_close"])).total_seconds() / 60.0
    if age > LAST_CALL_MIN:
        k, m, b = window_msg(tr, age, last, t); return [(k, m, b)]
    if age > NOW_MIN and not tr["last_call"]:
        return [window_msg(tr, age, last, t)]
    return []

def track(tr, candles5, t):
    """Advance tracking with new completed 5m candles. Returns result tuple or None."""
    p = tr["plan"]; sell = p["side"] == "sell"
    entry = tr["fill"] if tr.get("fill") else tr["signal_price"]
    risk = abs(entry - p["stop"])
    sf = parse(tr["scan_from"])
    new = [c for c in completed(candles5, 5, t) if c["t"] >= sf]
    for c in new:
        tr["scan_from"] = iso(c["t"] + timedelta(minutes=5))
        tr["best"] = min(tr["best"], c["l"]) if sell else max(tr["best"], c["h"])
        tr["worst"] = max(tr["worst"], c["h"]) if sell else min(tr["worst"], c["l"])
        hit_stop = c["h"] >= p["stop"] if sell else c["l"] <= p["stop"]
        hit_tgt = c["l"] <= p["target"] if sell else c["h"] >= p["target"]
        if hit_stop:      # stop wins ties (conservative)
            return "loss", -1.0, c["t"] + timedelta(minutes=5)
        if hit_tgt:
            return "win", abs(entry - p["target"]) / risk if risk else 0.0, c["t"] + timedelta(minutes=5)
    if t - parse(tr["signal_close"]) > timedelta(hours=TRACK_EXPIRE_H):
        lastc = completed(candles5, 5, t)[-1]["c"] if new or candles5 else entry
        r = ((entry - lastc) if sell else (lastc - entry)) / risk if risk else 0.0
        return "expired", r, t
    return None


def finish_trade(state, tr, result, r, when):
    p = tr["plan"]; sell = p["side"] == "sell"
    entry = tr["fill"] if tr.get("fill") else tr["signal_price"]
    risk = abs(entry - p["stop"]) or 1e-9
    mfe = ((entry - tr["best"]) if sell else (tr["best"] - entry)) / risk
    mae = ((tr["worst"] - entry) if sell else (entry - tr["worst"])) / risk
    sig = parse(tr["signal_close"])
    placed = "yes" if tr["placed"] else ("no" if tr["placed"] is False else "unknown")
    journal_add({"trade_id": tr["id"], "symbol": p["symbol"], "strategy": p["strategy"], "side": p["side"], "plan_id": p["id"],
                 "zone_touched_utc": tr.get("touched_at", ""), "signal_utc": iso(sig), "signal_price": fmt(p, tr["signal_price"]),
                 "planned_entry": fmt(p, p["entry"]), "stop": fmt(p, p["stop"]), "target": fmt(p, p["target"]),
                 "ratio_at_signal": "%.2f" % tr["ratio"],
                 "alert_delay_min": "%.1f" % ((parse(tr["first_alert"]) - sig).total_seconds() / 60.0),
                 "placed": placed, "user_fill": tr.get("fill") or "", "placed_utc": tr.get("placed_at") or "",
                 "resolved_utc": iso(when), "result": result, "r_multiple": "%.2f" % r, "mfe_r": "%.2f" % mfe,
                 "mae_r": "%.2f" % mae, "minutes_to_resolve": int((when - sig).total_seconds() / 60),
                 "weekday_paris": sig.astimezone(PARIS).strftime("%a"), "hour_paris": sig.astimezone(PARIS).hour})
    tag = "you placed it" if tr["placed"] else ("not taken" if tr["placed"] is False else "placement unknown")
    icon = {"win": "TARGET HIT", "loss": "STOP HIT", "expired": "EXPIRED (24h)"}[result]
    tg(state, "%s %+.1fR - %s (%s)\nSignal %s Paris, took %d min. Best move %.1fR, worst %.1fR against.\nLogged in your journal."
       % (icon, r, head_of(p), tag, pt(sig), int((when - sig).total_seconds() / 60), mfe, mae))


# ---------- Telegram inbound ----------
def handle_updates(state, trades):
    if not TG_TOKEN: return
    res = tg_call("getUpdates", {"offset": state.get("tg_offset", 0), "timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])})
    if not res or not res.get("ok"): return
    for u in res["result"]:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message"); cb = u.get("callback_query")
        chat = (msg or (cb or {}).get("message") or {}).get("chat", {}).get("id")
        if chat is None: continue
        if not (TG_CHAT_ENV or state.get("chat_id")):
            if msg and msg.get("chat", {}).get("type") == "private":
                state["chat_id"] = str(chat)
                tg(state, "Connected. I will send trade alerts here.\nCommands: /status  /journal  /fill SYMBOL PRICE\nYour chat id (optional, for the TELEGRAM_CHAT_ID secret): %s" % chat)
            continue
        if str(chat) != str(TG_CHAT_ENV or state.get("chat_id")): continue
        if cb:
            kind, _, tid = cb["data"].partition(":")
            tr = next((x for x in trades if x["id"] == tid), None)
            if tr:
                if kind == "P":
                    tr["placed"] = True; tr["placed_at"] = iso(now_utc()); tr["state"] = "tracking"
                    ans = "Logged as placed. I will track it and report the result."
                else:
                    tr["placed"] = False; tr["state"] = "tracking"
                    ans = "Logged as skipped. I will still track the outcome for your analysis."
            else: ans = "That signal is already closed."
            tg_call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": ans})
            continue
        text = (msg.get("text") or "").strip()
        if text.startswith("/journal"):
            rows = journal_rows(); tg(state, summary_text(rows)); tg_document(state, JOURNAL_PATH, "journal.csv")
        elif text.startswith("/status"):
            lines = ["Plans:"] + ["%s %s %s" % (pid, v.get("stage"), fmt_price(v.get("last_price"))) for pid, v in state["plans"].items()]
            lines += ["Open signals: %d" % len(trades)] + ["%s %s placed=%s" % (x["id"], x["state"], x["placed"]) for x in trades]
            tg(state, "\n".join(lines))
        elif text.startswith("/fill"):
            parts = text.split()
            try:
                sym, px = parts[1].upper(), float(parts[2])
                tr = next(x for x in reversed(trades) if x["plan"]["symbol"] == sym)
                tr["fill"] = px; tr["placed"] = True; tr["placed_at"] = tr.get("placed_at") or iso(now_utc()); tr["state"] = "tracking"
                tg(state, "Fill %s logged for %s. Tracking from your price." % (px, sym))
            except Exception:
                tg(state, "Usage: /fill EURUSD 1.0990 (needs an open signal on that symbol)")
        elif text.startswith("/start") or text.startswith("/help"):
            tg(state, "Commands: /status  /journal  /fill SYMBOL PRICE. Alerts only; I never place trades.")


def fmt_price(x): return "-" if x is None else str(x)


# ---------- main ----------
def plan_sig(p): return "%s|%s|%s|%s|%s" % (p["zone_lo"], p["zone_hi"], p["entry"], p["stop"], p["target"])


def main():
    t = now_utc()
    doc = load(PLANS_PATH, {"plans": [], "rev": "0"})
    plans = doc["plans"]
    state = load(STATE_PATH, {})
    for k, v in (("plans", {}), ("fetch", {}), ("credits", {}), ("trades", []), ("warned", {})):
        state.setdefault(k, v)
    trades = state["trades"]
    # keep state for unchanged plans, reset changed or new ones; open trades are never lost
    keep = {}
    for p in plans:
        old = state["plans"].get(p["id"])
        keep[p["id"]] = old if old and old.get("sig") == plan_sig(p) else \
            {"stage": "idle", "sig": plan_sig(p), "armed_at": iso(t - timedelta(hours=1))}
    state["plans"] = keep
    if state.get("rev") != doc.get("rev"):
        state["rev"] = doc.get("rev")
        if TG_TOKEN and (TG_CHAT_ENV or state.get("chat_id")):
            tg(state, "Plans refreshed (rev %s): %d active plans." % (doc.get("rev"), len(plans)))

    handle_updates(state, trades)

    if not market_open(t):
        print("market closed"); save(state); return

    today = t.strftime("%Y-%m-%d")
    used = state["credits"].get(today, 0)
    to_fetch = []

    def want(sym, every):
        lf = state["fetch"].get(sym)
        if (lf is None or t - parse(lf) >= timedelta(minutes=every)) and sym not in to_fetch: to_fetch.append(sym)

    for p in plans:
        ps = state["plans"][p["id"]]
        if ps["stage"] in ("cancelled", "done"): continue
        lp = ps.get("last_price")
        if ps["stage"] == "zone": want(p["symbol"], 5)
        elif lp is None: want(p["symbol"], 5)
        elif min(abs(lp - p["zone_lo"]), abs(lp - p["zone_hi"])) / lp <= NEAR_PCT: want(p["symbol"], NEAR_EVERY_MIN)
        else: want(p["symbol"], FAR_EVERY_MIN)
    for tr in trades:
        want(tr["plan"]["symbol"], 5 if tr["state"] == "window" else TRACK_EVERY_MIN)
    if used + len(to_fetch) > DAILY_CREDIT_CAP:
        print("daily credit cap reached"); to_fetch = []
    data = {}
    for i in range(0, len(to_fetch), 8):
        try: data.update(fetch(to_fetch[i:i + 8]))
        except Exception as e: print("fetch error", e)
    if to_fetch:
        state["credits"] = {today: used + len(to_fetch)}
        for s in data:
            if data[s]: state["fetch"][s] = iso(t)
    for s, v in data.items():
        if v is None and state["warned"].get(s) != today:
            state["warned"][s] = today
            tg(state, "No price data for %s (Twelve Data free plan may not include it). Plans on it cannot be watched." % s)
        if v and (t - v[-1]["t"]).total_seconds() / 60.0 > STALE_MIN + 5:
            data[s] = None   # stale feed: do not act on old candles

    for p in plans:
        c5 = data.get(p["symbol"])
        if c5:
            for kind, m, b in eval_plan(p, c5, state["plans"][p["id"]], t, state, trades):
                tg(state, m, b)
    for tr in list(trades):
        c5 = data.get(tr["plan"]["symbol"])
        if not c5: continue
        last = completed(c5, 5, t)[-1]["c"] if completed(c5, 5, t) else tr["signal_price"]
        if tr["state"] == "window":
            for kind, m, b in update_window(tr, last, t): tg(state, m, b)
        res = track(tr, c5, t)
        if res:
            finish_trade(state, tr, res[0], res[1], res[2]); trades.remove(tr)

    # daily journal backup after the Friday/weekday close window
    if t.weekday() < 5 and t.hour >= 21 and state.get("last_report") != today:
        state["last_report"] = today
        rows = journal_rows()
        if rows:
            tg(state, summary_text(rows)); tg_document(state, JOURNAL_PATH, "journal.csv backup " + today)
    save(state)


if __name__ == "__main__":
    main()
