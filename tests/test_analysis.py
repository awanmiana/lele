import json
import sqlite3
import unittest
from unittest.mock import patch

from lele.analysis import engine
from lele.core import registry


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.addCleanup(self.conn.close)
        self.eid = registry.upsert_entity(self.conn, "bank", "Example Bank", country="US")

    def metric(self, key, value, period="2024", source="annual report"):
        if isinstance(value, dict):
            value = json.dumps(value)
        registry.add_metric(self.conn, self.eid, key, value, period, source)

    def report(self):
        return engine.analyze_entity(self.conn, self.eid)

    def group(self):
        return self.report()["fundamentals"]["groups"][0]

    def indicator(self, name="debt_to_assets"):
        return next(i for i in self.group()["indicators"] if i["name"] == name)

    def test_actual_ratios_and_provenance(self):
        for key, value in {
            "total_assets": 1000, "total_liabilities": 800, "total_equity": 200,
            "net_income": 40, "revenue": 400, "cash": 100, "total_debt": 300,
            "currency": "USD", "unit": "millions", "scale": 1000000,
        }.items():
            self.metric(key, value)
        report = self.report()
        group = report["fundamentals"]["groups"][0]
        actual = {i["name"]: i["value"] for i in group["indicators"]}
        self.assertEqual(actual, {
            "liabilities_to_assets": 0.8, "equity_to_assets": 0.2,
            "debt_to_assets": 0.3, "debt_to_equity": 1.5, "net_margin": 0.1,
            "return_on_assets": 0.04, "return_on_equity": 0.2, "cash_to_assets": 0.1,
        })
        self.assertEqual(group["missing_metrics"], [
            "inventory_net", "long_term_debt_current", "long_term_debt_noncurrent",
            "net_cash_from_financing_activities", "net_cash_from_investing_activities",
            "net_cash_from_operating_activities", "short_term_borrowings",
        ])
        self.assertIn("scope/duration not verified", " ".join(group["warnings"]))
        for indicator in group["indicators"]:
            self.assertEqual(indicator["period"], "2024")
            self.assertEqual(indicator["source"], "annual report")
            for evidence in indicator["inputs"].values():
                self.assertEqual(evidence["currency"], "USD")
                self.assertEqual(evidence["unit"], "millions")
                self.assertEqual(evidence["scale"], "1000000")
        self.assertEqual(report["entity"]["name"], "Example Bank")
        self.assertEqual(report["provenance"]["input_api"], "registry.entity_payload")
        self.assertEqual(report, self.report())
        json.dumps(report, allow_nan=False)

    def test_entity_payload_is_used_without_writes(self):
        before = self.conn.total_changes
        with patch.object(registry, "entity_payload", wraps=registry.entity_payload) as payload:
            self.report()
        payload.assert_called_once_with(self.conn, self.eid)
        self.assertEqual(before, self.conn.total_changes)

    def test_no_cross_period_or_source_ratios(self):
        self.metric("assets", 100, "2023", "A")
        self.metric("debt", 50, "2024", "A")
        self.metric("debt", 20, "2023", "B")
        for group in self.report()["fundamentals"]["groups"]:
            self.assertTrue(all(i["value"] is None for i in group["indicators"]))
        self.metric("debt", 10, "2023", "A")
        values = [(g["period"], g["source"], i["value"])
                  for g in self.report()["fundamentals"]["groups"]
                  for i in g["indicators"] if i["status"] == "ok"]
        self.assertEqual(values, [("2023", "A", 0.1)])

    def test_missing_period_or_source_blocks_ratios(self):
        for period, source in [("", "A"), ("2024", ""), ("", "")]:
            with self.subTest(period=period, source=source):
                self.conn.execute("DELETE FROM metrics")
                self.metric("assets", 100, period, source)
                self.metric("debt", 20, period, source)
                self.assertIsNone(self.indicator()["value"])
                self.assertTrue(self.group()["warnings"])

    def test_metadata_mismatch_and_partial_metadata(self):
        for field, left, right in [("currency", "USD", "EUR"),
                                   ("unit", "millions", "units"),
                                   ("scale", 1000, 1), ("currency", "USD", None)]:
            with self.subTest(field=field, right=right):
                self.conn.execute("DELETE FROM metrics")
                self.metric("assets", {"value": 100, field: left})
                debt = {"value": 20}
                if right is not None:
                    debt[field] = right
                self.metric("debt", debt)
                self.assertIsNone(self.indicator()["value"])
                self.assertIn("metadata", self.indicator()["note"])

    def test_matching_json_metadata_and_companion_formats(self):
        for separator in ("_", ".", ":"):
            with self.subTest(separator=separator):
                self.conn.execute("DELETE FROM metrics")
                self.metric("assets", {"value": 100, "currency": "usd", "unit": "Millions"})
                self.metric("debt", 20)
                self.metric("total_debt" + separator + "currency", " USD ")
                self.metric("debt" + separator + "units", "millions")
                self.assertEqual(self.indicator()["value"], 0.2)

    def test_conflicting_group_or_attribute_metadata(self):
        self.metric("assets", 100)
        self.metric("debt", 20)
        self.metric("currency", "USD")
        self.metric("reporting_currency", "EUR")
        self.assertIsNone(self.indicator()["value"])
        self.assertIn("total_assets", self.group()["invalid_metrics"])
        self.conn.execute("DELETE FROM metrics WHERE k='reporting_currency'")
        registry.set_attr(self.conn, self.eid, "currency", "GBP")
        self.assertIsNone(self.indicator()["value"])

    def test_absent_metadata_is_explicit_assumption(self):
        self.metric("assets", 100)
        self.metric("debt", 20)
        self.assertEqual(self.indicator()["value"], 0.2)
        self.assertIn("assumes", " ".join(self.group()["warnings"]))

    def test_alias_conflict_is_unknown_independent_of_order(self):
        self.metric("assets", 100)
        self.metric("total_assets", 200)
        self.metric("debt", 20)
        self.assertIsNone(self.indicator()["value"])
        self.assertIn("total_assets", self.group()["invalid_metrics"])
        self.metric("total_assets", 100)
        self.assertEqual(self.indicator()["value"], 0.2)

    def test_invalid_nonfinite_and_malformed_values(self):
        for value in ("NaN", "inf", "-inf", "1e999", "unknown", "12,34", "1,2,3",
                      "{broken", {"value": True}, {"value": 100, "currency": None}):
            with self.subTest(value=value):
                self.conn.execute("DELETE FROM metrics")
                self.metric("assets", value)
                self.metric("debt", 20)
                self.assertIsNone(self.indicator()["value"])
                self.assertIn("total_assets", self.group()["invalid_metrics"])
                json.dumps(self.report(), allow_nan=False)

    def test_nonpositive_denominator_and_negative_income(self):
        self.metric("net_income", -10)
        for value in (0, -100):
            self.metric("revenue", value)
            self.assertIsNone(self.indicator("net_margin")["value"])
            self.assertIn("nonpositive", self.indicator("net_margin")["note"])
        self.metric("revenue", 100)
        self.assertEqual(self.indicator("net_margin")["value"], -0.1)
        self.metric("net_income", 0)
        self.assertEqual(self.indicator("net_margin")["value"], 0)

    def test_numeric_overflow_is_unknown(self):
        self.metric("assets", "1e-300")
        self.metric("debt", "1e300")
        self.assertIsNone(self.indicator()["value"])
        json.dumps(self.report(), allow_nan=False)

    def test_formatted_numbers_and_zero_numerator(self):
        self.metric("assets", "1,000.00")
        self.metric("debt", 0)
        self.assertEqual(self.indicator()["value"], 0)
        self.metric("debt", "2e2")
        self.assertEqual(self.indicator()["value"], 0.2)

    def test_missing_unsupported_and_bank_limitations(self):
        empty = self.report()
        self.assertEqual(empty["fundamentals"]["missing_metrics"], list(engine.SUPPORTED_METRICS))
        self.assertEqual(empty["sentiment"]["status"], "insufficient")
        self.assertIsNone(empty["sentiment"]["mean_score"])
        self.metric("profit", 10)
        self.metric("GDP", 100)
        self.metric("cet1_ratio", 0.15)
        self.assertTrue(all(i["value"] is None for i in self.group()["indicators"]))
        self.assertIn("profit", self.group()["unsupported_metric_keys"])
        caveats = " ".join(self.report()["caveats"])
        for term in ("CET1", "liquidity", "NPL", "solvency", "exploitation"):
            self.assertIn(term, caveats)

    def test_relationships_are_preserved_without_inferences(self):
        regulator = registry.upsert_entity(self.conn, "regulator", "Example Regulator")
        owner = registry.upsert_entity(self.conn, "company", "Example Owner")
        registry.add_edge(self.conn, regulator, "supervises", self.eid)
        registry.add_edge(self.conn, owner, "shareholder_of", self.eid)
        relations = self.report()["relationships"]
        self.assertEqual({r["rel"] for r in relations}, {"supervises", "shareholder_of"})
        self.assertTrue(all(set(r) == {"dir", "rel", "other_id", "other_name", "other_kind",
                                      "source_url", "observed_at", "evidence"}
                            for r in relations))

    def test_missing_entity_and_invalid_ids(self):
        self.assertFalse(engine.analyze_entity(self.conn, 999)["found"])
        for eid in (True, "1", 0, -1, None, 2**63):
            with self.subTest(eid=eid), self.assertRaises(ValueError):
                engine.analyze_entity(self.conn, eid)

    def test_record_stores_registry_signal_with_replayable_evidence(self):
        text = "The bank reported strong growth but losses."
        with patch.object(registry, "add_signal", wraps=registry.add_signal) as add:
            result = engine.record_sentiment(self.conn, self.eid, text, " news ", " item-1 ")
        add.assert_called_once()
        self.assertTrue(result["recorded"])
        row = registry.entity_payload(self.conn, self.eid)["signals"][0]
        self.assertEqual((row["source"], row["ref"]), ("news", "item-1"))
        evidence = json.loads(row["rationale"])
        self.assertEqual(evidence["method"], engine.SENTIMENT_METHOD)
        self.assertEqual(evidence["text"], text)
        self.assertEqual(evidence["analysis"], engine.analyze_sentiment(text))
        self.assertEqual(row["score"], result["score"])
        self.assertEqual(row["direction"], result["direction"])
        self.assertLessEqual(abs(row["score"]), 1)
        self.assertEqual(self.report()["sentiment"]["scored_signals"], 1)

    def test_record_does_not_commit(self):
        self.conn.commit()
        engine.record_sentiment(self.conn, self.eid, "strong growth", "news")
        self.conn.rollback()
        self.assertEqual(registry.entity_payload(self.conn, self.eid)["signals"], [])

    def test_record_validation_has_no_writes(self):
        cases = [
            (999, "growth", "news", ""), (True, "growth", "news", ""),
            (self.eid, "", "news", ""), (self.eid, " \n ", "news", ""),
            (self.eid, None, "news", ""), (self.eid, "growth", " ", ""),
            (self.eid, "growth", None, ""), (self.eid, "growth", "news", None),
            (self.eid, "x" * (engine.MAX_TEXT_LEN + 1), "news", ""),
            (self.eid, "growth", "x" * (engine.MAX_SOURCE_LEN + 1), ""),
            (self.eid, "growth", "news", "x" * (engine.MAX_REF_LEN + 1)),
        ]
        before = self.conn.total_changes
        for args in cases:
            with self.subTest(args=args[:1]), self.assertRaises(ValueError):
                engine.record_sentiment(self.conn, *args)
        self.assertEqual(before, self.conn.total_changes)

    def test_record_at_length_bounds(self):
        text = "growth" + " " * (engine.MAX_TEXT_LEN - 6)
        result = engine.record_sentiment(self.conn, self.eid, text,
                                         "s" * engine.MAX_SOURCE_LEN, "r" * engine.MAX_REF_LEN)
        self.assertTrue(result["recorded"])

    def test_insufficient_text_does_not_store_zero_signal(self):
        for text in ("銀行の利益", "le profit est stable", "shareholders meeting"):
            with self.subTest(text=text):
                result = engine.record_sentiment(self.conn, self.eid, text, "news")
                self.assertFalse(result["recorded"])
                self.assertIsNone(result["score"])
        self.assertEqual(registry.entity_payload(self.conn, self.eid)["signals"], [])

    def test_summary_excludes_other_methods_invalid_and_inconsistent_scores(self):
        engine.record_sentiment(self.conn, self.eid, "strong growth", "A")
        engine.record_sentiment(self.conn, self.eid, "losses", "B")
        rationale = json.dumps({"method": engine.SENTIMENT_METHOD})
        for score, direction, evidence in [(2, "positive", rationale),
                                            (float("inf"), "positive", rationale),
                                            (float("nan"), "neutral", rationale),
                                            (-1, "positive", rationale),
                                            (0.5, "positive", "other method"),
                                            (0, "neutral", "[]")]:
            registry.add_signal(self.conn, self.eid, "sentiment", score, direction, evidence, "X")
        registry.add_signal(self.conn, self.eid, "enforcement", -1, "negative")
        summary = self.report()["sentiment"]
        self.assertEqual(summary["scored_signals"], 2)
        self.assertEqual(summary["excluded_signals"], 6)
        self.assertEqual(summary["mean_score"], 0)
        self.assertEqual(summary["direction_counts"], {"positive": 1, "negative": 1, "neutral": 0})
        self.assertEqual(summary["sources"], ["A", "B"])
        self.assertIn("not representative", " ".join(summary["limitations"]))
        json.dumps(self.report(), allow_nan=False)

    def test_summary_respects_registry_payload_limit(self):
        for _ in range(105):
            engine.record_sentiment(self.conn, self.eid, "growth", "A")
        summary = self.report()["sentiment"]
        self.assertEqual(summary["recorded_signals"], 100)
        self.assertIn("100", " ".join(summary["limitations"]))


class SentimentTests(unittest.TestCase):
    def test_positive_negative_balanced_mentions(self):
        cases = [("strong growth growth", 1, "positive", 3, 0),
                 ("losses fraud", -1, "negative", 0, 2),
                 ("growth losses", 0, "neutral", 1, 1)]
        for text, score, direction, positive, negative in cases:
            with self.subTest(text=text):
                result = engine.analyze_sentiment(text)
                self.assertEqual(result["score"], score)
                self.assertEqual(result["direction"], direction)
                self.assertEqual(result["counts"]["positive"], positive)
                self.assertEqual(result["counts"]["negative"], negative)
                self.assertEqual(result, engine.analyze_sentiment(text))

    def test_tokens_not_substrings(self):
        result = engine.analyze_sentiment("The bank has lossless growthiness and profitableish results")
        self.assertIsNone(result["score"])
        self.assertEqual(result["counts"]["negative"], 0)
        self.assertEqual(result["counts"]["positive"], 0)

    def test_basic_negation_scope_contractions_and_double_negation(self):
        cases = [("not strong", -1), ("no losses", 1), ("isn’t strong", -1),
                 ("not not strong", 1), ("not strong. growth", 0),
                 ("not strong but growth", 0), ("not strong; growth", 0),
                 ("not the bank reported growth", 1)]
        for text, score in cases:
            with self.subTest(text=text):
                self.assertEqual(engine.analyze_sentiment(text)["score"], score)
        self.assertEqual(engine.analyze_sentiment("not strong")["negated"], ["strong"])

    def test_neutral_counts_are_occurrences_not_unique_words(self):
        result = engine.analyze_sentiment("bank bank growth growth")
        self.assertEqual(result["counts"], {"positive": 2, "negative": 0, "neutral": 2})
        self.assertEqual(result["words"], {"positive": ["growth"], "negative": [], "neutral": ["bank"]})
        self.assertEqual(result["confidence"], 0.5)
        self.assertIn("not predictive", " ".join(result["limitations"]))

    def test_empty_unsupported_and_uncertain_language(self):
        for text in ("", "  ", "12345", "銀行の利益", "прибыль", "le profit est stable",
                     "Der Profit ist gut", "El banco es fuerte", "銀行 growth", "profit incroyable"):
            with self.subTest(text=text):
                result = engine.analyze_sentiment(text)
                self.assertIn(result["status"], {"unknown", "insufficient"})
                self.assertIsNone(result["score"])
                self.assertIsNone(result["direction"])
                self.assertIsNone(result["confidence"])

    def test_criticism_and_relationship_terms_are_not_exploitation(self):
        result = engine.analyze_sentiment("criticism regulators shareholders ownership supervision")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["counts"], {"positive": 0, "negative": 0, "neutral": 5})
        self.assertIsNone(result["score"])

    def test_analysis_length_bound(self):
        result = engine.analyze_sentiment("growth " * engine.MAX_TEXT_LEN)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "text_too_long")


if __name__ == "__main__":
    unittest.main()
