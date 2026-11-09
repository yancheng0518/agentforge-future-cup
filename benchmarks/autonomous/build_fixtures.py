"""Rebuild static task bundles. This script never imports or runs candidates."""
from pathlib import Path
import ast
import json
import textwrap


ROOT = Path(__file__).resolve().parent


def source(text):
    return textwrap.dedent(text).lstrip()


CASES = {
    "sales_report": {
        "requirement": """Implement the two-module sales reporting library using only Python's standard library.
sales_csv.parse_sales(text) reads CSV with the exact header region,amount. CSV quoting is supported. Strip surrounding whitespace from each field. Ignore completely blank rows. A region must remain non-empty. Each amount must be a non-negative ordinary decimal with exactly two decimal digits; no sign or exponent. Return rows in input order as {\"region\": str, \"amount_cents\": int}. Never calculate money with binary floating point. Empty input, wrong header, wrong column count, missing region, or invalid amounts raise ValueError.
sales_report.summarize(rows) groups these rows by region and returns a list of {\"region\": str, \"total_cents\": int, \"count\": int}, sorted lexicographically by region, including zero totals. Empty rows produce [].
sales_report.report(text, min_total_cents=0) composes parse_sales and summarize, retaining groups whose total_cents >= min_total_cents. min_total_cents must be a non-negative int (bool is not accepted), otherwise raise ValueError. Both modules must work together; imports refer to sales_csv. Implement the incomplete scaffolds. Choose the decomposition, subagents and verification sequence yourself.""",
        "files": {
            "sales_csv.py": source('''
                """Sales CSV parsing; prices are integer cents."""

                def parse_sales(text):
                    raise NotImplementedError("Implement the requested CSV contract")
            '''),
            "sales_report.py": source('''
                """Aggregations and public report entry point."""

                def summarize(rows):
                    raise NotImplementedError("Implement aggregation")

                def report(text, min_total_cents=0):
                    raise NotImplementedError("Implement the composed report")
            '''),
        },
        "acceptance": source('''
            import unittest
            from bridge import call


            class SalesAcceptance(unittest.TestCase):
                def test_parse_preserves_order_and_cents(self):
                    self.assertEqual(call("sales_csv", "parse_sales", "region,amount\\nEast,0.10\\nWest,12.34\\nEast,0.20\\n"), [
                        {"region": "East", "amount_cents": 10}, {"region": "West", "amount_cents": 1234},
                        {"region": "East", "amount_cents": 20}])

                def test_csv_quoting_whitespace_blank_rows(self):
                    self.assertEqual(call("sales_csv", "parse_sales", 'region,amount\\n" North, East ", 2.50 \\n\\nSouth,0.00\\n'), [
                        {"region": "North, East", "amount_cents": 250}, {"region": "South", "amount_cents": 0}])

                def test_header_only(self):
                    self.assertEqual(call("sales_csv", "parse_sales", "region,amount\\n"), [])

                def test_reject_invalid_csv(self):
                    for text in ("", "amount,region\\n1.00,A", "region,amount\\nA,1.001", "region,amount\\nA,-1.00", "region,amount\\nA,1e2", "region,amount\\n,2.00", "region,amount\\nA,2.00,extra"):
                        with self.subTest(text=text), self.assertRaises(ValueError):
                            call("sales_csv", "parse_sales", text)

                def test_aggregate_sorted_repeated_regions(self):
                    rows = [{"region": "West", "amount_cents": 300}, {"region": "East", "amount_cents": 100}, {"region": "West", "amount_cents": 20}]
                    self.assertEqual(call("sales_report", "summarize", rows), [
                        {"region": "East", "total_cents": 100, "count": 1}, {"region": "West", "total_cents": 320, "count": 2}])

                def test_aggregate_zero_and_empty(self):
                    self.assertEqual(call("sales_report", "summarize", []), [])
                    self.assertEqual(call("sales_report", "summarize", [{"region": "Z", "amount_cents": 0}]), [{"region": "Z", "total_cents": 0, "count": 1}])

                def test_large_integer_money_stays_exact(self):
                    self.assertEqual(call("sales_report", "report", "region,amount\\nA,90071992547409.93\\nA,0.01\\n"), [{"region": "A", "total_cents": 9007199254740994, "count": 2}])

                def test_report_composes_modules(self):
                    self.assertEqual(call("sales_report", "report", "region,amount\\nB,1.20\\nA,0.10\\nB,2.30\\nA,0.20\\n"), [
                        {"region": "A", "total_cents": 30, "count": 2}, {"region": "B", "total_cents": 350, "count": 2}])

                def test_report_inclusive_threshold(self):
                    self.assertEqual(call("sales_report", "report", "region,amount\\nA,1.00\\nB,0.99\\nC,2.00\\n", min_total_cents=100), [
                        {"region": "A", "total_cents": 100, "count": 1}, {"region": "C", "total_cents": 200, "count": 1}])

                def test_report_empty_after_filter(self):
                    self.assertEqual(call("sales_report", "report", "region,amount\\nA,1.00\\n", min_total_cents=101), [])

                def test_invalid_threshold(self):
                    for threshold in (-1, True, 1.2, "100"):
                        with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                            call("sales_report", "report", "region,amount\\nA,1.00\\n", min_total_cents=threshold)

                def test_zero_threshold_keeps_zero(self):
                    self.assertEqual(call("sales_report", "report", "region,amount\\nZero,0.00\\n"), [{"region": "Zero", "total_cents": 0, "count": 1}])
        '''),
        "provenance": {
            "kind": "natural",
            "description": "Incomplete NotImplementedError scaffolds, not a deliberately faulty model output. Baseline failures are incomplete-scaffold evidence. Only failures of subsequently model-generated candidates qualify as natural generated-code failures.",
        },
    },
    "interval_library": {
        "requirement": """Implement a standard-library-only interval utility in intervals.py. All intervals are half-open [start,end], with integer endpoints; bool endpoints are invalid. Input is a list of two-element lists. Invalid shapes/types or end < start raise ValueError. Zero-length intervals are discarded.
normalize(intervals) returns sorted disjoint [start,end] lists, merging overlapping or touching intervals, with no zero-length entries. Empty input gives [].
contains(intervals, point) accepts an integer point (not bool), normalizes the intervals, and returns a bool indicating start <= point < end for at least one interval. Invalid points raise ValueError.
gaps(intervals, start, end) returns the uncovered portions inside the half-open window [start,end], sorted as lists. Normalize input first, clip coverage to the window, and return no zero-length gaps. The window endpoints follow the same integer/not-bool validation; end < start raises ValueError and start == end gives [].
Implement the incomplete scaffold. Select task decomposition, worker allocation and plan updates yourself. Do not infer required numbers of workers from the number of functions.""",
        "files": {
            "intervals.py": source('''
                """Half-open integer interval utilities."""

                def normalize(intervals):
                    raise NotImplementedError("Implement interval normalization")

                def contains(intervals, point):
                    raise NotImplementedError("Implement membership")

                def gaps(intervals, start, end):
                    raise NotImplementedError("Implement window gaps")
            '''),
        },
        "acceptance": source('''
            import unittest
            from bridge import call


            class IntervalAcceptance(unittest.TestCase):
                def test_normalize_empty_and_zero_length(self):
                    self.assertEqual(call("intervals", "normalize", []), [])
                    self.assertEqual(call("intervals", "normalize", [[2, 2], [0, 0]]), [])

                def test_normalize_sort_merge_touch_overlap(self):
                    self.assertEqual(call("intervals", "normalize", [[5, 8], [1, 3], [3, 5], [7, 10], [-3, -1]]), [[-3, -1], [1, 10]])

                def test_normalize_nested_duplicate_negative(self):
                    self.assertEqual(call("intervals", "normalize", [[-8, -2], [-7, -3], [-8, -2], [4, 6]]), [[-8, -2], [4, 6]])

                def test_reject_invalid_intervals(self):
                    for intervals in ([[3, 1]], [[False, 2]], [[1, 2.5]], [[1]], [[1, 2, 3]], ["ab"]):
                        with self.subTest(intervals=intervals), self.assertRaises(ValueError):
                            call("intervals", "normalize", intervals)

                def test_contains_half_open_boundaries(self):
                    for point, expected in ((1, True), (4, True), (5, False), (0, False)):
                        with self.subTest(point=point):
                            self.assertIs(call("intervals", "contains", [[1, 5]], point), expected)

                def test_contains_unsorted_and_empty(self):
                    self.assertIs(call("intervals", "contains", [[9, 12], [-3, 1]], -1), True)
                    self.assertIs(call("intervals", "contains", [], 0), False)

                def test_contains_reject_invalid_point(self):
                    for point in (True, 1.5, "2"):
                        with self.subTest(point=point), self.assertRaises(ValueError):
                            call("intervals", "contains", [[0, 5]], point)

                def test_gaps_clip_and_merge_coverage(self):
                    self.assertEqual(call("intervals", "gaps", [[-4, 2], [4, 6], [6, 8], [9, 15]], 0, 10), [[2, 4], [8, 9]])

                def test_gaps_empty_and_full_window(self):
                    self.assertEqual(call("intervals", "gaps", [], -2, 3), [[-2, 3]])
                    self.assertEqual(call("intervals", "gaps", [[-4, 8]], -2, 3), [])

                def test_gaps_outside_and_empty_window(self):
                    self.assertEqual(call("intervals", "gaps", [[-10, -5], [10, 20]], 0, 5), [[0, 5]])
                    self.assertEqual(call("intervals", "gaps", [[1, 3]], 2, 2), [])

                def test_gaps_reject_invalid_window(self):
                    for start, end in ((3, 2), (False, 2), (0, 2.5)):
                        with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                            call("intervals", "gaps", [], start, end)

                def test_large_integers(self):
                    low = 10 ** 20
                    self.assertEqual(call("intervals", "gaps", [[low + 1, low + 2]], low, low + 3), [[low, low + 1], [low + 2, low + 3]])
        '''),
        "provenance": {
            "kind": "natural",
            "description": "Incomplete NotImplementedError scaffold, no injected defect. Any scaffold baseline failure is separately classified; natural failure means a real-model candidate later failed independent acceptance.",
        },
    },
    "shipping_seeded": {
        "requirement": """Investigate and fix the existing shipping.py implementation. This is an explicitly seeded-defect validation case, not a natural model-error experiment. First request Docker verification of the supplied baseline, then use its actual result to decide the repair plan.
shipping_cost(weights, subtotal_cents, express=False) returns integer cents. weights is a list of strictly positive integer gram weights, excluding bool. subtotal_cents is a non-negative integer, excluding bool. express must be bool. Invalid arguments raise ValueError, even for an empty order. An empty weights list costs 0. A nonempty order with subtotal_cents >= 5000 gets free standard shipping. Otherwise the first 1000 grams cost 500 cents; every started additional 1000 grams costs 200 cents. Express adds 400 cents to any nonempty order, including an order eligible for free standard shipping.
The baseline deliberately uses floor division for additional kilograms, so non-multiple additional kilograms are undercharged. Keep this provenance explicit and keep the original baseline result. Repair from verification evidence and request final Docker verification before reporting completion. You determine the task assignments and do not need a named fixed fixer role.""",
        "files": {
            "shipping.py": source('''
                """Shipping quote. Fixture contains one disclosed rounding defect."""

                def shipping_cost(weights, subtotal_cents, express=False):
                    if not isinstance(weights, list) or any(type(w) is not int or w <= 0 for w in weights):
                        raise ValueError("weights must be positive integer grams")
                    if type(subtotal_cents) is not int or subtotal_cents < 0:
                        raise ValueError("subtotal must be non-negative integer cents")
                    if type(express) is not bool:
                        raise ValueError("express must be bool")
                    if not weights:
                        return 0
                    if subtotal_cents >= 5000:
                        base = 0
                    else:
                        additional = max(0, sum(weights) - 1000)
                        base = 500 + (additional // 1000) * 200
                    return base + (400 if express else 0)
            '''),
        },
        "acceptance": source('''
            import unittest
            from bridge import call


            class ShippingAcceptance(unittest.TestCase):
                def test_empty_order(self):
                    self.assertEqual(call("shipping", "shipping_cost", [], 0), 0)
                    self.assertEqual(call("shipping", "shipping_cost", [], 5000, express=True), 0)

                def test_under_and_exact_first_kilogram(self):
                    self.assertEqual(call("shipping", "shipping_cost", [1], 0), 500)
                    self.assertEqual(call("shipping", "shipping_cost", [400, 600], 4999), 500)

                def test_one_gram_over_first_kilogram(self):
                    self.assertEqual(call("shipping", "shipping_cost", [1001], 0), 700)

                def test_partial_second_kilogram(self):
                    self.assertEqual(call("shipping", "shipping_cost", [750, 750], 100), 700)

                def test_exact_additional_kilograms(self):
                    self.assertEqual(call("shipping", "shipping_cost", [2000], 0), 700)
                    self.assertEqual(call("shipping", "shipping_cost", [3000], 0), 900)

                def test_round_each_started_kilogram(self):
                    self.assertEqual(call("shipping", "shipping_cost", [2001], 0), 900)
                    self.assertEqual(call("shipping", "shipping_cost", [4200], 0), 1300)

                def test_free_threshold_is_inclusive(self):
                    self.assertEqual(call("shipping", "shipping_cost", [3000], 4999), 900)
                    self.assertEqual(call("shipping", "shipping_cost", [3000], 5000), 0)

                def test_express_surcharge(self):
                    self.assertEqual(call("shipping", "shipping_cost", [1000], 0, express=True), 900)
                    self.assertEqual(call("shipping", "shipping_cost", [3000], 5000, express=True), 400)

                def test_invalid_weights(self):
                    for weights in ([0], [-1], [True], [1.5], "1000"):
                        with self.subTest(weights=weights), self.assertRaises(ValueError):
                            call("shipping", "shipping_cost", weights, 0)

                def test_invalid_other_arguments_even_empty(self):
                    for subtotal in (-1, True, "5"):
                        with self.subTest(subtotal=subtotal), self.assertRaises(ValueError):
                            call("shipping", "shipping_cost", [], subtotal)
                    with self.assertRaises(ValueError):
                        call("shipping", "shipping_cost", [], 0, express="yes")
        '''),
        "provenance": {
            "kind": "seeded_defect",
            "description": "Disclosed fixture defect: additional shipping kilograms use floor division rather than ceiling. Baseline is expected to fail 3 of 10 test methods (one-gram-over, partial-second-kilogram, round-each-started-kilogram). This is not a naturally occurring model defect or an undisclosed post-generation injection.",
        },
    },
}


def main():
    for name, bundle in CASES.items():
        # Parse syntax only; candidate imports and execution are Docker-only.
        for path, content in bundle["files"].items():
            ast.parse(content, filename=path)
        tree = ast.parse(bundle["acceptance"], filename=name + "_acceptance.py")
        tests = sum(isinstance(item, ast.FunctionDef) and item.name.startswith("test_") for item in ast.walk(tree))
        target = ROOT / (name + ".json")
        target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{name}: {len(bundle['files'])} source files, {tests} test methods, {bundle['provenance']['kind']}")


if __name__ == "__main__":
    main()
