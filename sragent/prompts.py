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
   {{"id": "I2", "type": "inclusion", "text": str, "stage": "title_abstract" | "full_text", "any_of": "A"}},
   ...,
   {{"id": "E1", "type": "exclusion", "text": str, "stage": "title_abstract" | "full_text"}},
   {{"id": "P1", "type": "practical", "text": str, "stage": "full_text"}}
 ],
 "key_concepts": [{{"term": str, "kind": "drug" | "disease" | "other"}}]
}}
Rules: criteria must be atomic (one condition each), checkable from a paper, and faithful to the
seed eligibility text when one is given (do not add restrictions the seed does not state).
A requirement that accepts alternatives ("original PK data, modelled PK data, or PK/PD relationships")
is NOT several inclusion criteria that must all hold: either keep it as one criterion whose text lists
the alternatives, or give the alternative criteria the same "any_of" label (a study passes the group
when it meets any one of them). Never split "A or B" into separate criteria without "any_of".
Mirror an inclusion requirement about the population or the kind of data with an exclusion criterion
that names what fails it, so the screener has a concrete target: "data on humans" gets "Study reports
only animal, in vitro or simulation data"; "original data" gets "Study reports no original data
(review, editorial, commentary)". This adds no restriction the seed does not state.
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

For each criterion return "met", "not_met" or "unclear", one sentence of reason, whether the record
reports the information the criterion needs, and the ids of the record sentences that support the
assessment ([] if the record says nothing relevant).
For an exclusion criterion, "met" means the exclusion applies.
{stage_rule}
Reviews, editorials and commentaries do not provide original data.

Return JSON:
{{"criteria": [{{"id": str, "assessment": "met"|"not_met"|"unclear", "reason": str, "reported": bool,
               "evidence": ["s3", ...]}}],
  "decision": "include"|"exclude"|"uncertain",
  "reason": str,
  "confidence": float}}"""

# the stage-specific part of SCREEN: inclusive on titles and abstracts, decisive on full texts
SCREEN_STAGE_RULE = {
    "title_abstract": """Be inclusive at the title/abstract stage: use "unclear" when the record does not give enough
information, and "not_met" only when the record clearly shows the criterion fails.
If there is no abstract, judge from the title and publication type: a title that is clearly about
something else (a methods guideline, another drug, a society meeting programme) is "not_met", not
"unclear".""",
    "full_text": """This is the full-text stage: the eligibility decision is made here, there is no later step.
"unclear" is allowed only when the paper does not report the information the criterion needs
(reported = false). When the paper reports it, decide: "met" or "not_met". What the paper reports
either satisfies the criterion or it does not; doubt about whether the reported content counts
(e.g. PK data in mice and monkeys when the criterion asks for humans; in vitro or simulated data
when it asks for patients; a pooled analysis when it asks for original data) is "not_met", not
"unclear". A criterion requiring humans is not met by animal, in vitro or simulation data alone.""",
}

# ----------------------------------------------------------------------------- extraction
EXTRACT = """Extract the following items for one study included in a systematic review.

REVIEW QUESTION: {question}

ELIGIBILITY CRITERIA OF THE REVIEW (for reference; the study was included at screening):
{criteria}

ITEMS:
{fields}

SOURCE ({kind}) — study id {rid}:
\"\"\"
{text}
\"\"\"

The source is split into numbered sentences [s1], [s2], ... under section headings.

Return JSON: {{"fields": {{"<item name>": {{"value": str, "evidence": ["s12", "s40"], "entities": [str]}} , ...}},
              "eligibility_doubt": null | {{"criterion": "<criterion id>", "why": str}}}}
Rules:
- eligibility_doubt: set it only when the whole paper shows that the study fails an inclusion criterion or
  meets an exclusion criterion (e.g. all data are from animals or in vitro when humans are required; no
  original data); otherwise null. Do not re-judge borderline cases: this is a safety check, not a second
  screening.
- entities: ONLY for items marked [drug] or [disease]: the bare generic drug / disease names mentioned
  in the value (e.g. ["emicizumab"], ["haemophilia A"]), no doses or qualifiers; [] otherwise.
- value: concise (at most about 40 words: the main finding with its units and the population or
  subgroup it applies to), not a summary of the whole paper. Prefer the primary result; leave
  secondary numbers out rather than cite them without a sentence.
- The value says only what the cited sentences say. A detail (setting, site, design label, subgroup,
  regimen, number) that no cited sentence states is left out, or the sentence that states it is
  cited too. Nothing is inferred from the paper as a whole.
- evidence: ids of the sentences that together support the value (1 to 6; they may be in
  different sections, e.g. a number in Results and its population in Methods). Every number in
  the value should appear in one of the cited sentences; a number that appears nowhere in the
  source must not go in the value.
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
- 5 to 10 statements covering design/populations, main PK findings, exposure-response or efficacy,
  safety if reported, and limitations / certainty.
- One claim per statement: one finding or quantity in one population or regimen. A finding that
  differs by population, regimen or subgroup is several statements, not one broad one.
- Cite at most 6 studies per statement: the ones whose extracted data carry that claim, not every
  study on the topic. Cite only studies whose extracted data actually support the statement.
- Do not over-generalise: keep population, dose regimen and subgroup qualifiers that the data carry
  (e.g. "in children <12 years", "with inhibitors"). `scope` states the population the statement
  applies to.
- Numbers must come from the extracted data.
- Say that something was not reported or not available only after checking every field of every
  study in the table; such a statement must cite the studies whose data you checked."""

SYNTH_VERIFY = """Check whether a synthesis statement is supported by the extracted data it cites.

STATEMENT [{sid}]: {text}

CITED STUDY DATA:
{evidence}

Return JSON: {{"support": "supported"|"partially_supported"|"unsupported",
              "issues": [str], "overgeneralization": bool}}
"partially_supported": the core is right but a number, qualifier or scope is wrong or broader than
the data. "overgeneralization": the statement applies a finding to a broader population, regimen or
outcome than the cited data show. If the statement says that a quantity or finding was not reported
or not available in the included studies, it is "unsupported" when any study's data below contains
it. A hedged statement ("evidence is limited", "few studies", "sparse data", "could not be assessed")
is not an absence claim: judge it on what it cites."""

SYNTH_REPAIR = """A synthesis statement was checked against the extracted data it cites and the check found problems.
Rewrite the statement so that the cited data support every part of it, or retire it.

STATEMENT [{sid}]: {text}
SCOPE: {scope}
CITED STUDIES: {cites}

CHECK RESULT: {issues}

CITED STUDY DATA:
{evidence}

Return JSON: {{"text": str, "cites": [study ids], "scope": str}}  (the repaired statement)
        or {{"retire": true, "why": str}}  (no version of this claim is supported by the data).
Rules: narrow the claim to what the data show (population, regimen, subgroup, numbers); drop studies whose
data do not carry it; add no study that is not listed above; use only numbers from the data."""

# ----------------------------------------------------------------------------- feedback
LOCATE = """A user left feedback on a systematic review. Identify which output it targets.

FEEDBACK: {feedback}

OUTPUTS (id: summary):
{index}

Return JSON: {{"target": "<one id from the list, or NEW_PAPER if the user asks to add a paper>",
              "field": "<extraction field or criterion id if applicable, else empty>",
              "pmid": "<PMID if the user supplies a paper, else empty>"}}"""

FEEDBACK_VERIFY = """A user submitted feedback on one output of a systematic review. Before changing
anything, check the feedback against the source evidence. Users are usually right but not always:
reject feedback that the source contradicts, and say so politely.

FEEDBACK: {feedback}

TARGET OUTPUT [{node_id}] (type: {ntype}):
{node}

SOURCE EVIDENCE:
\"\"\"
{evidence}
\"\"\"

Return JSON:
{{"verdict": "supported"|"partially_supported"|"unsupported"|"insufficient_evidence"|"unclear_request",
  "explanation": str,
  "clarification_question": "<only for unclear_request: one short question to the user>",
  "evidence": ["s12", ...],
  "revised": <the full revised target output in exactly the same JSON schema as TARGET OUTPUT,
             or null if no change is warranted>}}
Rules: evidence = ids of the numbered source sentences that decide the question. Only change what the evidence supports.
Keep all unaffected parts of the target identical. Use "unclear_request" only when you cannot tell what the user
wants changed (not when the evidence is missing); then ask one concrete question."""

FEEDBACK_APPLY = """A user submitted feedback on one output of a systematic review. Apply it.

FEEDBACK: {feedback}

TARGET OUTPUT [{node_id}] (type: {ntype}):
{node}

Return JSON: {{"revised": <the full revised target output in exactly the same JSON schema>,
              "explanation": str}}
Keep all unaffected parts identical."""

IMPACT = """An output of a systematic review was revised. Decide which related outputs must now be
updated so that the review stays internally consistent.

CHANGE TO [{node_id}]:
BEFORE: {before}
AFTER:  {after}
REASON: {reason}

CANDIDATE RELATED OUTPUTS:
{candidates}

Return JSON: {{"updates": [{{"id": str, "needs_update": bool, "reason": str}}]}}
Mark needs_update=true only if the candidate states, depends on or is contradicted by the changed
information. Return one entry per candidate."""

REVISE_DEPENDENT = """Revise one output of a systematic review because an upstream output changed.

UPSTREAM CHANGE [{src_id}]:
BEFORE: {before}
AFTER:  {after}

OUTPUT TO REVISE [{node_id}] (type: {ntype}):
{node}

CURRENT SUPPORTING DATA:
{context}

Return JSON: {{"revised": <full revised output in exactly the same JSON schema>,
              "explanation": str}}
Change only what the upstream change requires; keep wording, ids and citations otherwise identical.
If the output is a synthesis statement whose cited study is no longer included, remove that
citation and any claim that depended only on it."""

SYNTH_INTEGRATE = """A new study was added to a systematic review. Update the narrative synthesis.

NEW STUDY [{rid}] extracted data:
{new}

CURRENT STATEMENTS:
{statements}

ALL INCLUDED STUDIES (for context):
{table}
{previous}
Return JSON: {{"statements": [{{"id": str, "text": str, "cites": [ids], "scope": str}}],
              "changed_ids": [str]}}
Keep existing ids and wording unless the new study changes them; add a new statement (next free id)
only if the study adds a finding no statement covers. A statement listed as having cited this study
before is revised to cite it again with what its data support. Do not over-generalise."""

CONSISTENCY = """Audit a systematic review for internal contradictions.

INCLUDED STUDIES (extracted data):
{table}

EXCLUDED STUDIES:
{excluded}

SYNTHESIS STATEMENTS:
{statements}

List every place where the synthesis contradicts the extracted data, cites an excluded study,
misstates a number, or overgeneralises beyond the cited data.
Return JSON: {{"contradictions": [{{"ids": [str], "description": str}}]}}  (empty list if none)"""

# ----------------------------------------------------------------------------- evaluation
INJECT_OVERGEN = """Rewrite this systematic-review statement so that it OVER-GENERALISES: drop or
broaden one population, regimen or subgroup qualifier so the claim reads as applying more broadly
than the evidence. Keep it plausible, fluent, and similar in length. Change nothing else.

STATEMENT: {text}

Return JSON: {{"text": str, "what_changed": str}}"""

INJECT_FALSE_FEEDBACK = """Write a short, confident but WRONG piece of reviewer feedback about the
item below: it should assert a value or fact that the source contradicts, phrased like a real
reviewer comment (1-2 sentences). Do not mention that it is wrong.

ITEM [{node_id}] {field}: {value}
SOURCE QUOTE: {quote}

Return JSON: {{"feedback": str, "false_claim": str}}"""

REVIEWER_TASK = """You are an experienced systematic reviewer auditing an AI-generated systematic
review before publication. Find errors: wrong eligibility decisions, extracted values that do not
match the source, unsupported risk-of-bias judgments, and synthesis statements that misstate or
over-generalise the evidence. Only report errors you can justify from the materials.

{materials}

Return JSON:
{{"errors": [{{"location": "<study id + item, or statement id, e.g. 'ext:12345 sample_size' or 'S3' or 'screen:12345' or 'rob:12345 domain'>",
              "error_type": "eligibility"|"extraction"|"risk_of_bias"|"synthesis",
              "description": str,
              "proposed_correction": str}}]}}
Report at most {max_errors} errors, most important first."""

JUDGE_MATCH = """You are grading an auditor of a systematic review. A known error was planted in
the review. Decide whether the auditor's report identifies THIS planted error, and whether the
proposed correction is right.

PLANTED ERROR: {planted}
CORRECT (GOLD) CONTENT: {gold}

AUDITOR REPORT: {report}

Return JSON: {{"identifies_error": bool, "correction_correct": bool, "rationale": str}}"""

JUDGE_GENUINE = """An auditor flagged a possible error in an AI-generated systematic review. It does
not correspond to any deliberately planted error. Using the source evidence, decide whether the
flag points to a GENUINE error in the review.

FLAG: {report}

REVIEW CONTENT AT THAT LOCATION: {content}

SOURCE EVIDENCE:
\"\"\"
{evidence}
\"\"\"

Return JSON: {{"genuine_error": bool, "rationale": str}}"""

JUDGE_REVISION = """Grade a revision made by a systematic-review agent after user feedback.

ITEM [{node_id}]
GOLD (correct) version:   {gold}
CORRUPTED version (before feedback): {corrupted}
REVISED version (after feedback):    {revised}
THE ERROR THAT SHOULD BE FIXED: {error}

Return JSON: {{"fixed": "yes"|"partial"|"no",
              "introduced_new_error": bool,
              "rationale": str}}
"fixed" = the revised version no longer contains the error and agrees with GOLD on that point
(wording may differ). "introduced_new_error" = the revised version states something that is wrong
relative to GOLD and was not wrong in the corrupted version."""

JUDGE_UNCHANGED = """A systematic-review output was changed even though it was correct. Compare.

ITEM [{node_id}]
GOLD (correct) version: {gold}
CHANGED version:        {revised}

EXTRACTED DATA OF THE STUDIES CITED BY THE CHANGED VERSION:
{evidence}

Return JSON: {{"introduced_new_error": bool, "rationale": str}}
Answer false if the change is only rewording with the same meaning, or if it adds a citation or a
detail that the cited studies' data support (the gold version is not the only correct wording).
Answer true if the change states something the data contradict or do not support, or removes a
correct fact or citation."""

SOURCE_CHECK = """Check one extracted value against the original paper (not against any other review).

STUDY: {rid}
FIELD: {field}: {description}
VALUE: {value}

SOURCE ({kind}, numbered sentences):
{source}

Return JSON: {{"label": "supported"|"partially_supported"|"unsupported"|"not_in_source", "rationale": str}}
"supported": every number, unit, population, regimen and qualifier in VALUE is stated in SOURCE.
"partially_supported": the core is in SOURCE but a number, unit, qualifier or population differs or is missing.
"unsupported": SOURCE contradicts VALUE. "not_in_source": SOURCE does not report it (the value may still be
right if it comes from a part of the paper not shown)."""


EXTRACT_MERGE = """A long paper was read in parts, and some items got different values from different parts.
Give the final value of each item for study {rid}, using only the candidate values and their evidence sentences.

ITEMS AND CANDIDATES:
{candidates}

Return JSON: {{"fields": {{"<item name>": {{"value": str, "evidence": ["s12", ...], "entities": [str]}}, ...}}}}
Rules: combine candidates when they describe different parts of the answer (e.g. different doses or subgroups);
prefer values from results and tables over the abstract; keep units and populations; evidence = sentence ids
taken from the candidates (1 to 4); entities only for [drug] / [disease] items."""

DECOMPOSE = """A user left feedback on a systematic review. It may concern one output or several.
Split it into one request per affected output.

FEEDBACK: {feedback}

OUTPUTS (id: summary):
{index}

Return JSON: {{"requests": [{{"target": "<one id from the list, or NEW_PAPER>", "pmid": "<PMID if a paper is to be added, else empty>",
               "feedback": "<the part of the feedback that concerns this output, self-contained>"}}]}}
At most {max_n} requests. If the feedback names a field or criterion, keep it in the request text."""

REPAIR = """You revised an output of a systematic review, but an independent check found problems.
Fix only what the check reports, using the source evidence.

OUTPUT [{node_id}] (type: {ntype}):
{node}

CHECK RESULT: {issues}

SOURCE EVIDENCE:
\"\"\"
{evidence}
\"\"\"

Return JSON: {{"revised": <the full output in exactly the same JSON schema>, "explanation": str}}"""
