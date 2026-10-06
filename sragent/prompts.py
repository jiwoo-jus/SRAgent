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
