"""All prompts in one place, so that they can be inspected, versioned and ablated.

Conventions
  * Every prompt asks for a single JSON object.
  * Sources are shown as numbered sentences [s1], [s2], ... grouped by section. Every claim
    cites the sentence ids that support it (evidence may be spread over several places).
    Ids are resolved to exact locations afterwards (sragent.evidence); no verbatim copying needed.
  * Prompts never reveal evaluation gold labels.
"""

SYSTEM_AGENT = (
    "You are a meticulous systematic-review methodologist working under PRISMA 2020 and Cochrane "
    "Handbook conventions. You only state what the provided sources support, you cite the "
    "numbered source sentences that support each claim, and you say 'NR' (not reported) instead of guessing. Respond with one JSON object."
)

# ----------------------------------------------------------------------------- planning
PLAN = """Draft a review protocol for the question below.

QUESTION: {question}

{seed}

Return JSON:
{{
 "title": str,
 "question": str,
 "pico": {{"population": str, "intervention_or_exposure": str, "comparator": str,
          "outcomes": [str], "study_designs": [str]}},
 "criteria": [
   {{"id": "I1", "type": "inclusion", "text": str, "stage": "title_abstract" | "full_text"}},
   ...,
   {{"id": "E1", "type": "exclusion", "text": str, "stage": "title_abstract" | "full_text"}},
   {{"id": "P1", "type": "practical", "text": str, "stage": "full_text"}}
 ],
 "key_concepts": [{{"term": str, "kind": "drug" | "disease" | "other"}}]
}}
Rules: criteria must be atomic (one condition each), checkable from a paper, and faithful to the
seed eligibility text when one is given (do not add restrictions the seed does not state).
Use "full_text" stage only for criteria that usually cannot be judged from an abstract.
Requirements about document availability or access (e.g. "full text accessible", "abstract available",
language of the publication) are type "practical": they are checked by the review team from
metadata, not judged from the paper's content.
key_concepts: the 2-6 core entities (generic drug names, diseases) used to build the search."""

SEARCH_QUERY = """Build a PubMed search strategy for this protocol.

PROTOCOL:
{protocol}

VALIDATED SYNONYMS FROM DATABASES (RxNorm / ChEMBL / PubChem / MONDO):
{synonyms}

Return JSON:
{{"concept_blocks": [{{"concept": str, "terms": [str]}}],
  "query": str,
  "rationale": str}}
Rules: one block per core concept, OR within a block, AND between blocks. Use [tiab] and, where
appropriate, MeSH [mh]. Prefer sensitivity (recall) over precision: a systematic review must not
miss eligible studies. Do not add design or language filters unless the protocol requires them."""

# ----------------------------------------------------------------------------- screening
SCREEN = """Assess this record against each eligibility criterion.

CRITERIA:
{criteria}

{synonyms}RECORD ({stage}):
{record}

For each criterion return "met", "not_met" or "unclear" and the ids of the record sentences that
support the assessment ([] if the record says nothing relevant).
For an exclusion criterion, "met" means the exclusion applies.
Be inclusive at the title/abstract stage: use "unclear" when the record does not give enough
information, and "not_met" only when the record clearly shows the criterion fails.
If there is no abstract, judge from the title and publication type: a title that is clearly about
something else (a methods guideline, another drug, a society meeting programme) is "not_met", not
"unclear". Reviews, editorials and commentaries do not provide original data.

Return JSON:
{{"criteria": [{{"id": str, "assessment": "met"|"not_met"|"unclear", "evidence": ["s3", ...]}}],
  "decision": "include"|"exclude"|"uncertain",
  "reason": str,
  "confidence": float}}"""

# ----------------------------------------------------------------------------- extraction
EXTRACT = """Extract the following items for one study included in a systematic review.

REVIEW QUESTION: {question}

ITEMS:
{fields}

SOURCE ({kind}) — study id {rid}:
\"\"\"
{text}
\"\"\"

The source is split into numbered sentences [s1], [s2], ... under section headings.

Return JSON: {{"fields": {{"<item name>": {{"value": str, "evidence": ["s12", "s40"], "entities": [str]}} , ...}}}}
Rules:
- entities: ONLY for items marked [drug] or [disease]: the bare generic drug / disease names mentioned
  in the value (e.g. ["emicizumab"], ["haemophilia A"]), no doses or qualifiers; [] otherwise.
- value: concise, include units and the population/subgroup the value applies to.
- evidence: ids of the sentences that together support the value (1 to 4; they may be in
  different sections, e.g. a number in Results and its population in Methods).
- If the item is not reported, value = "NR" and evidence = [].
- Do not use outside knowledge about the study."""

ROB = """Draft a risk-of-bias / study-quality assessment for this study. A human reviewer will
check it, so every judgment must be traceable to the text.

DOMAINS:
{domains}

STUDY EXTRACTION:
{extraction}

SOURCE ({kind}) — study id {rid}:
\"\"\"
{text}
\"\"\"

Return JSON:
{{"domains": [{{"domain": str, "judgment": "low"|"some_concerns"|"high"|"unclear",
               "rationale": str, "evidence": ["s5", ...]}}],
  "overall": "low"|"some_concerns"|"high"|"unclear"}}
Rules: evidence = ids of the numbered source sentences behind the judgment (1 to 4). If the source does not contain the information needed,
use "unclear" (not "low")."""

# ----------------------------------------------------------------------------- synthesis
SYNTH = """Write the narrative synthesis for a systematic review.

QUESTION: {question}

INCLUDED STUDIES (extracted data; ids in brackets):
{table}

RISK OF BIAS (overall per study):
{rob}

Return JSON:
{{"statements": [{{"id": "S1", "text": str, "cites": [study ids], "scope": str}}]}}
Rules:
- 4 to 8 statements covering design/populations, main PK findings, exposure-response or efficacy,
  safety if reported, and limitations / certainty.
- Every statement must cite the study ids that support it; cite only studies whose extracted data
  actually support the statement.
- Do not over-generalise: keep population, dose regimen and subgroup qualifiers that the data carry
  (e.g. "in children <12 years", "with inhibitors"). `scope` states the population the statement
  applies to.
- Numbers must come from the extracted data."""

SYNTH_VERIFY = """Check whether a synthesis statement is supported by the extracted data it cites.

STATEMENT [{sid}]: {text}

CITED STUDY DATA:
{evidence}

Return JSON: {{"support": "supported"|"partially_supported"|"unsupported",
              "issues": [str], "overgeneralization": bool}}
"partially_supported": the core is right but a number, qualifier or scope is wrong or broader than
the data. "overgeneralization": the statement applies a finding to a broader population, regimen or
outcome than the cited data show."""

# ----------------------------------------------------------------------------- feedback