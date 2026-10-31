#!/usr/bin/env python3
"""Indian results reader for trader-analyst. Standard library only.

Reads a listed-company results XBRL instance the user supplies (NSE or BSE
company results XBRL, or any file in the same shape), resolves facts by local
element name so the taxonomy prefix does not matter (in-ca, in-capmkt,
in-bse-fin and the consolidated taxonomies all work), and reports what the
company actually filed: revenue, expenses, profit, EPS, derived margins and
growth, and what is missing. Every number is INR; no network calls, no PDF
parsing, no invented figures.

Fact resolution: dimensioned contexts (segments) are parsed but never used for
the consolidated rows, a slice cannot stand in for the company total. Among
undimensioned duplicates for the same element, the longest period ending at
the latest date is the current period; duplicates sharing one span must agree
with the majority, otherwise the disagreement is reported as a conflict and
the smallest context id breaks the tie deterministically. Nil or absent facts
are reported missing, never zero-filled.
"""

import argparse
import datetime
import json
import sys
import xml.etree.ElementTree as ET

DUR = "duration"
INST = "instant"
NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"

# fact local name -> kind of value: money (INR), pershare (INR per share),
# count (number of shares)
NAMES = {
    "RevenueFromOperations": "money",
    "Revenue": "money",
    "OtherIncome": "money",
    "TotalIncome": "money",
    "EmployeeBenefitExpense": "money",
    "FinanceCosts": "money",
    "DepreciationDepletionAndAmortisationExpense": "money",
    "OtherExpenses": "money",
    "ProfitBeforeTax": "money",
    "TaxExpense": "money",
    "ProfitLossForPeriod": "money",
    "ProfitLossFromContinuingOperations": "money",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "pershare",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "pershare",
    "PaidUpValueOfEquityShareCapital": "money",
    "NumberOfShares": "count",
}
# preferred concept for each report slot, first that exists wins
REVENUE = ("RevenueFromOperations", "Revenue")
PROFIT = ("ProfitLossForPeriod", "ProfitLossFromContinuingOperations")
BASIC_EPS = "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"
DILUTED_EPS = "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"
OPERATING = ("EmployeeBenefitExpense", "FinanceCosts",
             "DepreciationDepletionAndAmortisationExpense", "OtherExpenses")


class FactsError(Exception):
    pass


def local(tag):
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------- filing

def parse_file(path):
    """Parse the XBRL instance at path; malformed XML raises FactsError."""
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise FactsError("%s is not well-formed XML: %s" % (path, exc))


def read_contexts(root):
    """{id: {"kind": DUR|INST, "start", "end", "instant", "entity", "dims"}}.

    Segment members inside <entity> become dimension entries so dimensioned
    facts (segments) can be told apart from the untagged consolidated totals.
    """
    out = {}
    for node in root.iter():
        if local(node.tag) != "context":
            continue
        ctx = {"entity": None, "dims": []}
        period = node.find("period")
        if period is not None:
            start, end = period.find("startDate"), period.find("endDate")
            instant = period.find("instant")
            if instant is not None:
                ctx["kind"], ctx["instant"] = INST, (instant.text or "").strip()
            elif start is not None and end is not None:
                ctx["kind"] = DUR
                ctx["start"] = (start.text or "").strip()
                ctx["end"] = (end.text or "").strip()
        ident = node.find("./entity/identifier")
        if ident is not None:
            ctx["entity"] = (ident.text or "").strip()
        for member in node.findall("./entity/segment/member"):
            dim, val = local(member.tag), (member.findtext("value") or "").strip()
            dom = member.find("domain")
            if dom is not None:
                dim, val = local(dom.tag), (dom.text or "").strip()
            if val or dim:
                ctx["dims"].append((dim, val))
        out[node.get("id")] = ctx
    return out


def read_units(root):
    """{id: unit} with units in the INR, INR/shares, shares, pure shapes."""
    out = {}
    for node in root.iter():
        if local(node.tag) != "unit":
            continue
        ms = node.findall("./measure")
        if ms:
            out[node.get("id")] = local(ms[0].text or "")
            continue
        num = node.find("./divide/unitNumerator/measure")
        den = node.find("./divide/unitDenominator/measure")
        if num is not None and den is not None:
            out[node.get("id")] = "%s/%s" % (local(num.text or ""),
                                             local(den.text or ""))
        else:
            out[node.get("id")] = "unknown"
    return out


def read_facts(root, units):
    """[(name, kind, value, context id, unit)] for the Indian fact set."""
    out = []
    for node in root.iter():
        name = local(node.tag)
        kind = NAMES.get(name)
        if kind is None or node.get("contextRef") is None:
            continue
        text = (node.text or "").strip()
        if not text or node.get(NIL) == "true":
            continue  # nil or empty fact: reported missing, never zero-filled
        try:
            value = float(text)
        except ValueError:
            raise FactsError("fact %s has a non-numeric value %r" % (name, text))
        out.append((name, kind, value, node.get("contextRef"),
                    units.get(node.get("unitRef") or "", "unknown")))
    return out


# ---------------------------------------------------------------- resolution

def period_len(cid, contexts):
    ctx = contexts[cid]
    if ctx.get("kind") != DUR:
        return 0
    return (datetime.date.fromisoformat(ctx["end"]) -
            datetime.date.fromisoformat(ctx["start"])).days + 1


def majority_value(vals):
    """The value most facts agree on, or None when there is no majority."""
    count = {}
    for v in vals:
        count[v] = count.get(v, 0) + 1
    best, n = None, 0
    for v, c in count.items():
        if c > n or (c == n and best is not None and v < best):
            best, n = v, c
    return best if 2 * n > len(vals) else None


def pick(vals, contexts):
    """Deterministic choice among duplicates of one span.

    A strict-majority value wins (its smallest context id represents it);
    otherwise the disagreement is a conflict and the smallest context id
    breaks the tie. Returns (value, context id).
    """
    maj = majority_value([c[2] for c in vals])
    if maj is not None:
        for c in sorted(vals, key=lambda c: c[3]):
            if c[2] == maj:
                return c[2], c[3]
    c = sorted(vals, key=lambda c: c[3])[0]
    return c[2], c[3]


def resolve(root):
    """Read a filing into {"periods": {...}, "conflicts": [...], "best_end"}.

    periods maps each period end to {fact name: {"value", "context", "start"}}
    for undimensioned duration facts; within one end the longest span
    (smallest start) stands for the element, so a year-to-date column wins
    over a same-date quarter column and the period label matches the number.
    """
    contexts = read_contexts(root)
    units = read_units(root)
    facts = read_facts(root, units)
    spans = {}  # end -> name -> start -> [facts]
    for fact in facts:
        name, _kind, _value, cid, _unit = fact
        ctx = contexts[cid]
        if ctx.get("kind") != DUR or ctx["dims"]:
            continue  # instants and dimensioned (segment) facts are not
            # consolidated results rows
        spans.setdefault(ctx["end"], {}).setdefault(name, {}).setdefault(
            ctx["start"], []).append(fact)
    if not spans:
        raise FactsError("no duration facts for a results period in the XBRL file")
    periods, conflicts = {}, []
    for end in sorted(spans):
        row = {}
        for name in sorted(spans[end]):
            for start, vals in sorted(spans[end][name].items()):
                values = sorted(set(v for _, _, v, _, _ in vals))
                if len(values) > 1:
                    conflicts.append({"name": name, "period": end,
                                      "values": values,
                                      "contexts": sorted(c[3] for c in vals)})
            start = min(spans[end][name])  # longest span for this end
            value, ctxid = pick(spans[end][name][start], contexts)
            row[name] = {"value": value, "context": ctxid, "start": start}
        periods[end] = row
    return {"periods": periods, "conflicts": conflicts, "best_end": max(periods)}


# ---------------------------------------------------------------- metrics

def get(row, name):
    entry = row.get(name) if row else None
    return entry["value"] if entry else None


def concept(row, names):
    for n in names:
        if row.get(n):
            return n
    return None


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 2)


def growth(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return round(100.0 * (cur - prev) / abs(prev), 2)


def metric_row(row):
    """{"slot": {"concept": ..., "value": ...}} for every slot that exists."""
    rev = concept(row, REVENUE)
    prof = concept(row, PROFIT)
    m = {}
    if rev:
        m["revenue"] = {"concept": rev, "value": get(row, rev)}
    if row.get("OtherIncome") is not None:
        m["other_income"] = {"concept": "OtherIncome",
                             "value": get(row, "OtherIncome")}
    if row.get("TotalIncome") is not None:
        m["total_income"] = {"concept": "TotalIncome",
                             "value": get(row, "TotalIncome")}
    expenses = {n: get(row, n) for n in OPERATING if row.get(n) is not None}
    if expenses:
        m["expenses"] = expenses
        if row.get("TotalIncome") is not None:
            op = get(row, "TotalIncome") - sum(expenses.values())
            m["operating_profit"] = {"concept": "TotalIncome minus expenses",
                                     "value": round(op, 2)}
    if prof:
        m["profit"] = {"concept": prof, "value": get(row, prof)}
    if row.get("ProfitBeforeTax") is not None:
        m["profit_before_tax"] = {"concept": "ProfitBeforeTax",
                                  "value": get(row, "ProfitBeforeTax")}
    if row.get("TaxExpense") is not None:
        m["tax_expense"] = {"concept": "TaxExpense", "value": get(row, "TaxExpense")}
    if row.get(BASIC_EPS) is not None:
        m["eps_basic"] = {"concept": "EPS basic (INR)", "value": get(row, BASIC_EPS)}
    if row.get(DILUTED_EPS) is not None:
        m["eps_diluted"] = {"concept": "EPS diluted (INR)",
                            "value": get(row, DILUTED_EPS)}
    if prof and get(row, "NumberOfShares"):
        m["eps_from_profit"] = {"concept": "profit / NumberOfShares (INR)",
                                "value": round(get(row, prof) /
                                               get(row, "NumberOfShares"), 2)}
    if prof and rev:
        m["net_margin"] = {"concept": "profit / revenue",
                           "value": margin(get(row, prof), get(row, rev))}
    if row.get("PaidUpValueOfEquityShareCapital") is not None and get(row, "NumberOfShares"):
        m["paid_up_value_per_share"] = {
            "concept": "PaidUpValueOfEquityShareCapital / NumberOfShares (INR)",
            "value": round(get(row, "PaidUpValueOfEquityShareCapital") /
                           get(row, "NumberOfShares"), 2)}
    return m


def find_prior(periods, best_end, names, cur_len, tag, end_date):
    """Comparable prior-period value for a metric, or None.

    Comparable: an undimensioned duration ending before the current one, with
    a span within 15 days of the current span, and ending within 45 days of
    the previous period boundary (qoq) or within 30 days of one year earlier
    (yoy). A full-year fact is never borrowed as a quarter.
    """
    if cur_len is None:
        return None
    best = None
    for end in sorted(periods):
        if end >= best_end:
            continue
        row = periods[end]
        name = concept(row, names)
        if name is None or not row[name].get("start"):
            continue
        entry = row[name]
        end_d = datetime.date.fromisoformat(end)
        ln = (end_d - datetime.date.fromisoformat(entry["start"])).days + 1
        if abs(ln - cur_len) > 15:
            continue
        if tag == "qoq":
            if (end_date - end_d).days > cur_len + 45:
                continue
        else:
            target = end_date - datetime.timedelta(days=365)
            if abs((end_d - target).days) > 30:
                continue
        best = entry["value"]
    return best


def compare(periods, best_end, rev, prof):
    """QoQ and YoY for revenue and profit against comparable prior periods.

    When the file carries no comparable prior period the growth is None and
    the missing period is named, never guessed.
    """
    out, missing = {}, []
    end_date = datetime.date.fromisoformat(best_end)
    for label, names in (("revenue", rev), ("profit", prof)):
        row = periods[best_end]
        cur_name = concept(row, names)
        entry = row.get(cur_name) if cur_name else None
        cur = entry["value"] if entry else None
        cur_len = None
        if entry and entry.get("start"):
            cur_len = (end_date -
                       datetime.date.fromisoformat(entry["start"])).days + 1
        for tag in ("qoq", "yoy"):
            key = "%s_%s" % (label, tag)
            out[key] = None
            if cur is None:
                continue  # the metric itself is missing; analyse reports it
            prior = find_prior(periods, best_end, names, cur_len, tag, end_date)
            if prior is None:
                missing.append("no comparable prior period in the file for %s %s"
                               % (label, tag))
                continue
            out[key] = growth(cur, prior)
    return out, missing


# ---------------------------------------------------------------- report

def analyse(root, symbol, announcement=None, source=None):
    """The whole report as a dict: period, facts, derived, missing, conflicts."""
    parsed = resolve(root)
    best_end = parsed["best_end"]
    row = parsed["periods"][best_end]
    facts = metric_row(row)
    rev = concept(row, REVENUE)
    prof = concept(row, PROFIT)
    derived, missing = compare(parsed["periods"], best_end, REVENUE, PROFIT)
    op_margin = None
    if facts.get("operating_profit") is not None and get(row, "TotalIncome"):
        op_margin = margin(facts["operating_profit"]["value"],
                           get(row, "TotalIncome"))
    if facts.get("profit") is not None and facts.get("revenue"):
        derived["net_margin"] = margin(facts["profit"]["value"],
                                       facts["revenue"]["value"])
    if op_margin is not None:
        derived["operating_margin"] = op_margin
    else:
        derived["operating_margin"] = None
    if rev is None:
        missing.append("revenue (RevenueFromOperations / Revenue)")
    if prof is None:
        missing.append("profit (ProfitLossForPeriod / "
                       "ProfitLossFromContinuingOperations)")
    if row.get(BASIC_EPS) is None:
        missing.append("EPS (BasicEarningsLossPerShareFromContinuing"
                       "AndDiscontinuedOperations)")
    if row.get("NumberOfShares") is None:
        missing.append("NumberOfShares (share count, needed for derived EPS)")
    start = row[rev].get("start") if rev else None
    if start is None:
        for entry in row.values():
            if entry.get("start"):
                start = entry["start"]
                break
    return {
        "symbol": symbol,
        "period": {"start": start, "end": best_end,
                   "label": announcement or "results"},
        "unit": "INR crore",
        "facts": facts,
        "derived": derived,
        "missing": missing,
        "conflicts": parsed["conflicts"],
        "source": source or "XBRL results file supplied by the user",
    }


def _cr(v):
    return "-" if v is None else "%.2f" % (v / 1e7)


def _p(v):
    return "-" if v is None else "%+.2f%%" % v


ORDER = ("revenue", "other_income", "total_income", "expenses",
         "operating_profit", "profit_before_tax", "tax_expense", "profit",
         "eps_basic", "eps_diluted", "eps_from_profit",
         "paid_up_value_per_share")
LABELS = {
    "revenue": "Revenue from operations",
    "other_income": "Other income",
    "total_income": "Total income",
    "expenses": "Expenses",
    "operating_profit": "Operating profit (derived)",
    "profit_before_tax": "Profit before tax",
    "tax_expense": "Tax expense",
    "profit": "Profit for the period",
    "eps_basic": "EPS, basic (INR)",
    "eps_diluted": "EPS, diluted (INR)",
    "eps_from_profit": "EPS from profit / shares (INR, derived)",
    "paid_up_value_per_share": "Paid-up value per share (INR)",
}
PER_SHARE = ("eps_basic", "eps_diluted", "eps_from_profit",
             "paid_up_value_per_share")


def render(symbol, report):
    per, facts, der = report["period"], report["facts"], report["derived"]
    out = ["%s, results for the %s to %s (INR crore except per-share data)." % (
        symbol, per.get("start") or "start not tagged", per["end"])]
    if per.get("label") and per["label"] != "results":
        out.append("Announcement: %s" % per["label"])
    out.append("")
    out.append("Filed (XBRL):")
    for key in ORDER:
        entry = facts.get(key)
        if entry is None:
            continue
        if key == "expenses":
            names = {"EmployeeBenefitExpense": "employee costs",
                     "FinanceCosts": "finance costs",
                     "DepreciationDepletionAndAmortisationExpense": "depreciation",
                     "OtherExpenses": "other expenses"}
            items = ", ".join("%s %s" % (names[n], _cr(v))
                              for n, v in sorted(entry.items()))
            out.append("  %-40s %s" % (LABELS[key], items))
        elif key in PER_SHARE:
            out.append("  %-40s %.2f" % (LABELS[key], entry["value"]))
        else:
            out.append("  %-40s %s" % (LABELS[key], _cr(entry["value"])))
    out.append("")
    out.append("Derived:")
    out.append("  operating margin                         %s" % _p(der.get("operating_margin")))
    out.append("  net margin                               %s" % _p(der.get("net_margin")))
    out.append("  revenue QoQ                              %s" % _p(der.get("revenue_qoq")))
    out.append("  revenue YoY                              %s" % _p(der.get("revenue_yoy")))
    out.append("  profit QoQ                               %s" % _p(der.get("profit_qoq")))
    out.append("  profit YoY                               %s" % _p(der.get("profit_yoy")))
    if report["missing"]:
        out.append("")
        out.append("Missing (not tagged in this file, never zero-filled):")
        for m in report["missing"]:
            out.append("  %s" % m)
    if report["conflicts"]:
        out.append("")
        out.append("Conflicts (same fact, same span, different values):")
        for c in report["conflicts"]:
            out.append("  %s for %s: %s in contexts %s" % (
                c["name"], c["period"],
                ", ".join(_cr(v) for v in c["values"]),
                ", ".join(c["contexts"])))
    out.append("")
    out.append("Source: %s. Facts come only from the XBRL file; announcement"
               " text is a label, never a number." % report["source"])
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read an Indian results XBRL file: filed numbers, margins,"
                    " growth, what is missing. No network calls.")
    ap.add_argument("symbol", help="NSE symbol, e.g. RELIANCE")
    ap.add_argument("--xbrl", required=True, help="path to the results XBRL file")
    ap.add_argument("--announcement",
                    help="announcement text, used only as a period label")
    ap.add_argument("--announcement-file",
                    help="file with announcement text, used only as a period label")
    ap.add_argument("--json", action="store_true",
                    help="print the report as one JSON object")
    ap.add_argument("--source", help="override the reported source string")
    args = ap.parse_args(argv)
    if args.announcement and args.announcement_file:
        print("trader-analyst: use --announcement or --announcement-file, not both",
              file=sys.stderr)
        return 2
    text = args.announcement
    if args.announcement_file:
        try:
            with open(args.announcement_file, encoding="utf-8", errors="replace") as fh:
                text = " ".join(fh.read().split())
        except OSError as exc:
            print("trader-analyst: cannot read %s: %s" % (args.announcement_file, exc),
                  file=sys.stderr)
            return 2
    try:
        root = parse_file(args.xbrl)
        report = analyse(root, args.symbol.upper(), announcement=text,
                         source=args.source)
    except FactsError as exc:
        print("trader-analyst: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("trader-analyst: cannot read %s: %s" % (args.xbrl, exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(args.symbol.upper(), report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
