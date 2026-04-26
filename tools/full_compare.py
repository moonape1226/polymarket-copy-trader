#!/usr/bin/env python3
"""6h BS vs ours trade-by-trade comparison with latency.

Groups raw trades into actions (5s window same asset+side), matches BS to ours,
buckets latency into chain-feed-fast (<30s) vs reconcile-slow (>=30s), and
flags sizing anomalies.

Usage:
  python3 tools/full_compare.py [--hours 6]
"""
import argparse, time, requests

BS = "0x331bf91c132af9d921e1908ca0979363fc47193f"
US = "0x216eEe4DC3808a3f90A3E9612C7FF2f09DA3fDa6"


def fetch(addr, since):
    rows, off = [], 0
    while True:
        url = f"https://data-api.polymarket.com/activity?user={addr}&limit=500&offset={off}"
        batch = requests.get(url, timeout=15).json()
        if not batch: break
        rows.extend(batch)
        if batch[-1].get("timestamp",0) < since: break
        off += 500
        if off > 5000: break
    return [r for r in rows if r.get("type")=="TRADE" and r.get("timestamp",0) >= since]


def group(trades):
    trades = sorted(trades, key=lambda x: x["timestamp"])
    out = []
    for t in trades:
        key = (t.get("asset"), (t.get("side") or "").upper())
        if out and out[-1]["key"]==key and t["timestamp"]-out[-1]["last_ts"] <= 5:
            out[-1]["sh"] += float(t.get("size") or 0)
            out[-1]["usd"] += float(t.get("usdcSize") or 0)
            out[-1]["last_ts"] = t["timestamp"]
        else:
            out.append({
                "key": key, "asset": t.get("asset"),
                "title": (t.get("title") or "?")[:48],
                "side": (t.get("side") or "").upper(),
                "first_ts": t["timestamp"], "last_ts": t["timestamp"],
                "sh": float(t.get("size") or 0),
                "usd": float(t.get("usdcSize") or 0),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    args = ap.parse_args()
    since = time.time() - args.hours*3600

    bs = fetch(BS, since); us = fetch(US, since)
    bs_acts = group(bs); us_acts = group(us)

    print(f"BS raw: {len(bs)} → actions: {len(bs_acts)}")
    print(f"US raw: {len(us)} → actions: {len(us_acts)}")
    print()
    print(f"{'BS time':>9s} {'side':4s} {'BS sh':>8s} {'BS$':>7s} {'BS px':>7s} | {'lat':>6s} {'our 1st':>9s} {'our sh':>8s} {'our$':>7s} {'our px':>7s} {'sh%':>5s} | title")
    print("-"*150)

    fast, slow, miss, matched = [], [], 0, []
    for b in bs_acts:
        bts = time.strftime('%H:%M:%S', time.localtime(b["first_ts"]))
        bs_px = b["usd"]/b["sh"] if b["sh"] else 0
        ms = [u for u in us_acts
              if u["key"]==b["key"]
              and u["first_ts"] >= b["first_ts"] - 5
              and u["first_ts"] <= b["first_ts"] + 3600]
        if not ms:
            print(f"{bts} {b['side']:4s} {b['sh']:8.2f} ${b['usd']:6.2f} ${bs_px:6.4f} | {'MISS':>6s} {'':9s} {'':8s} {'':7s} {'':7s} {'':5s} | {b['title']}")
            miss += 1; continue
        lat = ms[0]["first_ts"] - b["first_ts"]
        u_first = time.strftime('%H:%M:%S', time.localtime(ms[0]["first_ts"]))
        u_sh = sum(m["sh"] for m in ms)
        u_usd = sum(m["usd"] for m in ms)
        u_px = u_usd/u_sh if u_sh else 0
        sh_pct = (u_sh / b["sh"] * 100) if b["sh"] else 0
        (fast if lat < 30 else slow).append(lat)
        print(f"{bts} {b['side']:4s} {b['sh']:8.2f} ${b['usd']:6.2f} ${bs_px:6.4f} | {lat:5.0f}s {u_first:>9s} {u_sh:8.2f} ${u_usd:6.2f} ${u_px:6.4f} {sh_pct:4.0f}% | {b['title']}")
        matched.append((b, ms, lat))

    print()
    print(f"Total: {len(bs_acts)} actions | matched: {len(bs_acts)-miss} | missed: {miss}")
    if fast: print(f"Fast (<30s, chain feed): {len(fast)} avg {sum(fast)/len(fast):.1f}s")
    if slow: print(f"Slow (≥30s, reconcile):  {len(slow)} avg {sum(slow)/len(slow):.0f}s max {max(slow):.0f}s")

    print()
    print("Sizing anomalies (|sh%-100| > 15%):")
    any_anom = False
    for b, ms, lat in matched:
        u_sh = sum(m["sh"] for m in ms)
        pct = u_sh / b["sh"] * 100 if b["sh"] else 0
        if abs(pct - 100) > 15:
            any_anom = True
            kind = "OVER" if pct > 115 else "UNDER"
            print(f"  {kind:5s} {time.strftime('%H:%M:%S', time.localtime(b['first_ts']))} {b['side']:4s} BS {b['sh']:.2f}sh ours {u_sh:.2f}sh ({pct:.0f}%) lat={lat:.0f}s | {b['title']}")
    if not any_anom: print("  (none)")


if __name__ == "__main__":
    main()
