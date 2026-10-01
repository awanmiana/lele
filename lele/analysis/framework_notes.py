"""A cited record of how money is actually allocated, and what the evidence says.

This exists because the question "how would a trader or a multi-millionaire trade
this asset?" has a bad answer, and the honest answer is a research finding rather
than advice. Every entry below is a *documented position with a primary source*,
paired with the documented criticism of it. Nothing here is a recommendation, a
signal, a position, or an input to any other module in this project.

Three rules govern the catalogue, and all three exist because the trading-education
internet is the worst-sourced material in finance.

1. *A claim is listed only with a source that was actually read.* Each entry names
   where the claim comes from and grades it: `primary` for a journal article,
   shareholder letter, statute or official release; `secondary` for something
   reached only through a summary; `vendor` for an interested party.
2. *What could not be sourced is listed as excluded, with the reason.* That list is
   longer than it should be and it is the more useful half of this module, because
   the excluded items are exactly the ones that circulate with the most confidence
   and the least provenance.
3. *Criticism is mandatory.* An entry without a documented counter-result would be
   marketing.

The reason this is a module rather than a document is that a claim in prose decays
silently. Held as data, it can be checked, cited, and removed when it is corrected.
"""

METHOD = "framework_notes_v1"

#: Evidence grades, weakest last. Nothing with grade `unverified` is in `NOTES`.
GRADES = ("primary", "secondary", "vendor")

#: Heterogeneous by design: most fields are text, `identifier` is text or None,
#: and `bears_on` is a list of topic slugs. A precise annotation would be a
#: dataclass over a table that is never queried by field shape.
NOTES: tuple[dict, ...] = (
    {
        "key": "wealth_concentration",
        "title": "The best-performing 4% of listed companies explain the entire net "
                 "gain for the US stock market since 1926",
        "attribution": "Bessembinder, S.J.",
        "source": "Do Stocks Outperform Treasury Bills?, Journal of Financial Economics "
                  "129(3):440-457, 2018",
        "identifier": "doi:10.1016/j.jfineco.2018.06.004",
        "url": "https://doi.org/10.1016/j.jfineco.2018.06.004",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "The best-performing 4% of listed companies account for the net gain "
                   "for the entire US stock market since 1926; other stocks collectively "
                   "matched Treasury bills.",
        "what_it_does_not_establish": "It does not identify which 4%, and it is not a claim "
                                      "that concentrated picking is achievable. It states the "
                                      "base rate: the median stock underperforms cash, so "
                                      "concentration is the only route to outperformance and "
                                      "also the only route to total loss of the search.",
        "bears_on": ["security_selection", "index_investing", "concentration"],
    },
    {
        "key": "trading_is_hazardous",
        "title": "The most active retail households underperformed the market by more "
                 "than six percentage points a year",
        "attribution": "Barber, B.M. and Odean, T.",
        "source": "Trading is Hazardous to Your Wealth, Journal of Finance 55(2):773-806, 2000",
        "identifier": "doi:10.1111/0022-1082.00226",
        "url": "https://doi.org/10.1111/0022-1082.00226",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "From 66,465 households at a large discount broker over 1991-1996, the "
                   "most active households earned 11.4% annually while the market returned "
                   "17.9%. The average household earned 16.4% and turned over 75% of its "
                   "portfolio annually. The authors attribute this to overconfidence.",
        "what_it_does_not_establish": "It does not show that every trader's activity is "
                                      "harmful, and it measures a specific broker's clients in "
                                      "a specific period. It does establish that the *sign* of "
                                      "the activity-to-return relationship is negative for "
                                      "this population, which is the opposite of the premise "
                                      "behind building a tool that invites frequent action.",
        "bears_on": ["turnover", "signal_frequency", "retail_behaviour"],
    },
    {
        "key": "day_trader_penalty",
        "title": "Losses of individual day traders trace to aggressive orders, while "
                 "passive orders are profitable",
        "attribution": "Barber, B.M., Lee, D., Liu, W. and Odean, T.",
        "source": "Do Day Traders Rationally Learn About Their Ability?, Journal of Financial "
                  "Markets 18:1-24, 2014 (and related work on the same dataset)",
        "identifier": "doi:10.1016/j.finmar.2013.05.006",
        "url": "https://doi.org/10.1016/j.finmar.2013.05.006",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "In Taiwan full-market data, the aggregate portfolio of individual "
                   "investors suffers an annual performance penalty of 3.8 percentage "
                   "points, equal to 2.2% of Taiwan's GDP, with the losses traced almost "
                   "entirely to aggressive orders; institutions earned about 1.5 points "
                   "annually over the same period.",
        "what_it_does_not_establish": "It is one market and one retail population. The "
                                      "transferable point is narrower and about product "
                                      "design: a signal that invites an immediate market "
                                      "order sits on the losing side of this result.",
        "bears_on": ["order_type", "latency", "signal_frequency"],
    },
    {
        "key": "multiple_testing_floor",
        "title": "A published factor needs a t-statistic above 3.0, not 1.96",
        "attribution": "Harvey, C.R., Liu, Y. and Zhu, H.",
        "source": "...and the Cross-Section of Expected Returns, Review of Financial Studies "
                  "29(1):5-68, 2016",
        "identifier": "doi:10.1093/rfs/hhv059",
        "url": "https://doi.org/10.1093/rfs/hhv059",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "Most published findings in this literature do not survive a "
                   "multiple-testing correction, and the authors propose a higher bar of "
                   "roughly 3.0 for a newly discovered factor.",
        "what_it_does_not_establish": "A t-statistic is not a guarantee of a live edge "
                                      "after costs, and the bar is itself a convention. It "
                                      "does establish that the conventional 1.96 threshold is "
                                      "too permissive once many variants have been tried.",
        "bears_on": ["backtesting", "signal_selection"],
    },
    {
        "key": "anomaly_replication_failure",
        "title": "82% of 452 published anomalies fail a corrected significance hurdle, "
                 "and the survivors are weaker than claimed",
        "attribution": "Hou, K., Xue, C. and Zhang, L.",
        "source": "Replicating Anomalies, Review of Financial Studies 33(5):2019-2133, 2020",
        "identifier": "doi:10.1093/rfs/hhy131",
        "url": "https://doi.org/10.1093/rfs/hhy131",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "65% of 452 anomalies cannot clear a t-value of 1.96; raising the hurdle "
                   "to 2.78 at the 5% level raises the failure rate to 82%. Even replicated "
                   "anomalies have much smaller economic magnitudes than first reported.",
        "what_it_does_not_establish": "It does not prove the market is efficient or that no "
                                      "anomaly survives. It is the strongest single argument "
                                      "that a research tool must record its own multiple "
                                      "testing and refuse to present an uncorrected result as "
                                      "a finding.",
        "bears_on": ["backtesting", "multiple_testing", "signal_selection"],
    },
    {
        "key": "backtest_overfitting",
        "title": "Standard hold-out techniques are unreliable for investment backtests, "
                 "and overfitting is quantifiable",
        "attribution": "Bailey, D., Borwein, J., Lopez de Prado, M. and Zhu, Q.",
        "source": "The Probability of Backtest Overfitting",
        "identifier": "ssrn:2326253",
        "url": "https://ssrn.com/abstract=2326253",
        "grade": "primary",
        "source_kind": "working paper",
        "finding": "Hold-out techniques tend to be unreliable and inaccurate in the context "
                   "of investment backtests; the authors propose the probability of "
                   "backtest overfitting via combinatorially symmetric cross-validation.",
        "what_it_does_not_establish": "It prescribes a measurement, not a conclusion about "
                                      "any particular strategy. Its value to a tool is that the "
                                      "quantity is computable and can be reported alongside "
                                      "any performance figure.",
        "bears_on": ["backtesting"],
    },
    {
        "key": "volatility_managed_portfolios",
        "title": "Scaling exposure down when volatility is high raised Sharpe ratios "
                 "in the original sample",
        "attribution": "Moreira, A. and Muir, T.",
        "source": "Volatility-Managed Portfolios, Journal of Finance 72(4):1611-1644, 2017",
        "identifier": "doi:10.1111/jofi.12513",
        "url": "https://doi.org/10.1111/jofi.12513",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "Portfolios that take less risk when volatility is high produce large "
                   "alphas and increase Sharpe ratios, which the authors argue rules out "
                   "conventional risk-based explanations.",
        "what_it_does_not_establish": "The abstract does not state a specific multiple for "
                                      "the Sharpe improvement, and figures commonly quoted "
                                      "for one are not in the abstract. Treat any single "
                                      "number here as unverified until read in the paper.",
        "bears_on": ["volatility_targeting", "risk_parity", "position_sizing"],
        "superseded_by": "volatility_managed_replication_failure",
    },
    {
        "key": "volatility_managed_replication_failure",
        "title": "Volatility-managed portfolios did not survive out-of-sample replication",
        "attribution": "Cederburg, O., O'Doherty, J., Wang, X. and Yan, Y.",
        "source": "On the performance of volatility-managed portfolios, Journal of Financial "
                  "Economics 138(1):95-117, 2020",
        "identifier": "doi:10.1016/j.jfineco.2020.04.015",
        "url": "https://doi.org/10.1016/j.jfineco.2020.04.015",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "Volatility-managed portfolios do not systematically outperform their "
                   "corresponding unmanaged portfolios in direct comparisons; the authors "
                   "attribute the original result to structural instability in the underlying "
                   "spanning regressions.",
        "what_it_does_not_establish": "It does not prove volatility management is useless in "
                                      "every context, and separate work reports that such "
                                      "strategies can fail to survive transaction costs. It "
                                      "does establish that a strategy published in a top "
                                      "journal by serious academics did not replicate -- which "
                                      "is the strongest available argument against shipping "
                                      "an in-sample performance figure without an "
                                      "out-of-sample protocol.",
        "bears_on": ["volatility_targeting", "backtesting", "position_sizing"],
        "supersedes": "volatility_managed_portfolios",
    },
    {
        "key": "allocation_is_the_primary_driver",
        "title": "Asset allocation is the primary driver of a portfolio's risk and return",
        "attribution": "Vanguard",
        "source": "Vanguard published research and strategy material on asset allocation "
                  "and on equity asset location",
        "identifier": None,
        "url": "https://corporate.vanguard.com/content/corporatesite/us/en/corp/who-we-are/"
               "sustainability/investment-management.html",
        "grade": "vendor",
        "source_kind": "issuer research",
        "finding": "Asset allocation, rather than the choice of individual securities, is "
                   "stated to be the primary determinant of portfolio risk and return.",
        "what_it_does_not_establish": "This is the house view of the largest index provider "
                                      "in the world, on its own commercial terms. It is "
                                      "recorded because it is the industry's own statement of "
                                      "where its product sits, not because it is independent "
                                      "evidence. The direction is corroborated by the "
                                      "concentration and turnover findings above.",
        "bears_on": ["allocation", "fee_drag"],
    },
    {
        "key": "rules_based_systems_are_published",
        "title": "A complete, dated, freely published rule set exists for a "
                 "trend-following system",
        "attribution": "Curtis Faith, for the original Turtle rules",
        "source": "The original Turtle rules, published by a participant",
        "identifier": None,
        "url": "https://www.originalturtles.org/originalturtlerules.htm",
        "grade": "primary",
        "source_kind": "first-party publication by a participant",
        "finding": "A breakout entry system with position sizing on the N (ATR), "
                   "pyramiding rules, and portfolio and correlation limits was published in "
                   "full and free. The durable part is the risk architecture, not the entry.",
        "what_it_does_not_establish": "It is a historical account of one group's results, "
                                      "not an audited track record, and it is not a claim that "
                                      "the rules currently work. Trend following suffered a "
                                      "severe drawdown in 2008 precisely because volatility "
                                      "parity and trend are not independent risk sources in a "
                                      "crash -- which is the reason trend is recorded here "
                                      "with a drawdown profile rather than as a diversifier.",
        "bears_on": ["trend_following", "position_sizing", "risk_budget"],
    },
    {
        "key": "risk_premiums_are_balanced_and_funded",
        "title": "The variance risk premium: implied variance exceeds realized variance "
                 "on average, and the premium is collected by the seller",
        "attribution": "Carr, J. and Wu, Y. (2009); Bollerslev, Tauchen and Zhou (2009)",
        "source": "Variance Risk Premiums, Review of Financial Studies 22(3):1311-1341, 2009; "
                  "Expected stock returns and variance risk premia, Review of Economic "
                  "Studies 22(11):4463-4492, 2009",
        "identifier": "doi:10.1093/rfs/hhm075",
        "url": "https://doi.org/10.1093/rfs/hhm075",
        "grade": "primary",
        "source_kind": "peer-reviewed article",
        "finding": "Variance-swap excess returns are not explained by the standard equity "
                   "and momentum factors, which the authors read as evidence that investors "
                   "pay for the transfer of variance as a good in its own right -- variance "
                   "behaving as an asset class rather than merely as a risk.",
        "what_it_does_not_establish": "A premium existing is not a statement that it is "
                                      "currently harvestable, net of the balance sheet "
                                      "required to fund a payoff that arrives rarely and is "
                                      "large when it does. Historical profitability is not "
                                      "capacity-constrained, post-crisis profitability. The "
                                      "structure is recorded; the trade is not recommended, "
                                      "and no average premium level is quoted here because no "
                                      "primary source for one was read.",
        "bears_on": ["derivatives", "volatility_as_asset"],
    },
)

#: Claims that circulate widely and that this project deliberately does not make,
#: because no primary source was reached. Absence here is not a judgement that the
#: claim is false; it is a record that the project could not stand behind it.
EXCLUDED: tuple[dict, ...] = (
    {"claim": "The Paul Tudor Jones 1/5/6 rule",
     "reason": "No primary source reached: not in a book, not in a documented interview "
               "transcript. Its substance is generic trend-following logic, which is recorded "
               "under rules_based_systems_are_published with a source that was read.",
     "where_it_circulates": "trading education material"},
    {"claim": "Volatility-managed portfolios doubled the Sharpe ratio",
     "reason": "The abstract says Sharpe ratios increased; it does not state a doubling. The "
               "specific figures are in the paper's tables, which were not read. Common as a "
               "citation for a number the cited source does not contain in that form.",
     "where_it_circulates": "secondary summaries of Moreira and Muir"},
    {"claim": "Halving the equity allocation doubles the required return",
     "reason": "A 2023 Vanguard paper commonly cited for this could not be located. The "
               "underlying relationship is real, but the project will not attach a number to "
               "a citation it has not read.",
     "where_it_circulates": "wealth-management blogs"},
    {"claim": "Diversification disappears in a crisis (Linder, UBS)",
     "reason": "The paper returned an error on every attempt and no verifiable mirror was "
               "found. The correlation-regime point is supported by other material recorded "
               "here, but not by that document.",
     "where_it_circulates": "risk-management decks"},
    {"claim": "Private-equity returns by vintage from Jay Cooke's Capital Returns",
     "reason": "The underlying dataset was not accessed, so no multiple is quoted. What is "
               "supported is a liquidity-timing distribution, not a headline return.",
     "where_it_circulates": "allocator marketing"},
    {"claim": "Bogle's Sharpe-ratio difference between cost-noisy and cost-aware indexing",
     "reason": "Frequently quoted figures circulate widely; the original source was not "
               "reached. The cost argument itself is recorded under "
               "allocation_is_the_primary_driver as a vendor position.",
     "where_it_circulates": "index-provider marketing"},
    {"claim": "Greenwald's five-step value test, Bernhard's simple numbers, Damodaran's "
              "valuation framework, the academic critique of the magic formula, Kluger's "
              "IVOL",
     "reason": "Real books and real ideas, but no primary or scholarly source was read in "
               "this session. Paraphrasing them as rules would reproduce exactly the failure "
               "this module exists to prevent.",
     "where_it_circulates": "book summaries and course material"},
    {"claim": "Specific quotes attributed to Buffett, Munger, Soros, Dalio or Klarman",
     "reason": "Almost everything widely attributed to these investors online is secondary "
               "paraphrase, frequently miscontextualised. Berkshire's shareholder letters are "
               "a genuine primary source and are public; a rule stated from a letter is "
               "acceptable here, an unsourced quotation is not.",
     "where_it_circulates": "quotation aggregators"},
    {"claim": "Dalio's All Weather portfolio weights and returns",
     "reason": "Bridgewater's own material describes the approach qualitatively; specific "
               "weights were not read. The approach is also not a recommendation this project "
               "makes.",
     "where_it_circulates": "product marketing"},
    {"claim": "The wash-sale window and its ETF and crypto specifics",
     "reason": "Primary IRS guidance was not retrieved. This is a compliance parameter and "
               "must be read from the IRS, not from this file.",
     "where_it_circulates": "broker and tax blogs"},
)

METHODOLOGICAL_NOTES = (
    "The independent finding across several of these sources is that *measured* "
    "performance is worse than *claimed* performance: activity underperforms, most "
    "published anomalies fail correction, a top-journal volatility result did not "
    "replicate. A tool that reports a calibrated estimate with its sample, its costs, its "
    "multiple-testing correction and its unknowns is more useful than one that emits a "
    "confident directional call, because the confident call is the specific thing the "
    "record shows to be unreliable.",

    "Size changes which risk binds, and it changes what the edge is. At small size the "
    "binding risk is behavioural and documented as costly. At family-office and "
    "institutional size it is fee drag, tax location, manager access, capacity and "
    "illiquidity tolerance -- documented in this project's own corpus (Form ADV, Form 13F, "
    "N-PORT, Form D, lobbying and sanctions records), not in the price series. A single "
    "recommendation surface offered across all sizes is therefore mis-specified.",

    "No published evidence was found for reliably superior directional accuracy at any "
    "very short horizon. The absence of evidence is not proof of impossibility, so the "
    "project's pre-registered target is left in place at 0.9 and is recorded as an "
    "aspiration rather than a gate, on the grounds that a target which cannot be met "
    "provides no gradient and is exactly the kind of constant that gets quietly lowered.",

    "Nothing in this module is consumed by any estimator, detector, score or command. It "
    "is a reference, kept as data so that each claim can be cited, checked and removed "
    "when it is corrected.",
)

NOT_A_SIGNAL = (
    "This module produces no signal, no score, no ranking and no position.",
    "Nothing here is investment, tax or legal advice.",
    "A framework entry is a documented position with a citation, not a validated "
    "technique, and an entry's presence is not an endorsement of it.",
    "The volatility and threshold machinery elsewhere in this project measures how much "
    "an asset moved. It does not predict where it goes next.",
)


def list_notes(grade: str = "", topic: str = "") -> list[dict]:
    """Notes filtered by evidence grade or by the question they bear on."""
    if grade and grade not in GRADES:
        raise ValueError(f"grade must be one of {', '.join(GRADES)}")
    rows = []
    for note in NOTES:
        if grade and note["grade"] != grade:
            continue
        if topic and topic not in note["bears_on"]:
            continue
        rows.append(dict(note))
    return rows


def get_note(key: str) -> dict | None:
    for note in NOTES:
        if note["key"] == key:
            return dict(note)
    return None


def excluded() -> list[dict]:
    return [dict(item) for item in EXCLUDED]


def report() -> dict:
    """The whole reference layer, with its own limits attached."""
    by_grade: dict[str, int] = {}
    for note in NOTES:
        by_grade[note["grade"]] = by_grade.get(note["grade"], 0) + 1
    return {
        "method": METHOD,
        "note_count": len(NOTES),
        "by_grade": dict(sorted(by_grade.items())),
        "excluded_count": len(EXCLUDED),
        "notes": [dict(note) for note in NOTES],
        "excluded": excluded(),
        "methodological_notes": list(METHODOLOGICAL_NOTES),
        "not_a_signal": list(NOT_A_SIGNAL),
        "limitations": [
            "Every grade is a judgement about what was read, not about whether the claim is "
            "true. A `primary` grade means the source was reached, not that the finding "
            "replicated.",
            "The excluded list is not exhaustive. It records what this project declined to "
            "assert, which is a sample of the unattributable claims in circulation.",
            "No figure of expected return, Sharpe ratio or price target appears anywhere in "
            "this module, and none may be derived from it.",
        ],
    }
