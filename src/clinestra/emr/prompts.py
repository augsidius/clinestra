"""Prompts of the EMR canvas."""
from string import Template

EMR_TRANSCRIPTION_PROMPT = Template(
  """
 # Medical Transcript to EMRU Conversion

Convert voice transcripts to EMRU syntax. Fix errors, standardize terminology, map to template structure.

---

## STEP 0: INPUT VALIDATION — RUN FIRST, BEFORE ANYTHING ELSE

Check the transcript input before any processing.

**Return `NO_UPDATE_REQUIRED` immediately and stop if the input is any of the following:**

| Condition | Examples |
|---|---|
| Empty or null | `""`, `null`, `" "`, no input provided |
| Non-speech audio only | `[silence]`, `[background noise]`, `[music]` |
| Zero clinical content | Greetings only, scheduling only, payment only, chitchat only |
| Unintelligible | `[inaudible]`, `???`, STT failure output |

**Do not attempt extraction. Do not generate any EMRU commands. Output only:**
```
NO_UPDATE_REQUIRED
```

Only proceed to STEP 1 if the transcript contains at least one clinical fact (symptom, diagnosis, medication, vital sign, investigation, history item, or clinical plan).

---

## STEP 1: DETECT INPUT MODE

| | Mode A: Dictation | Mode B: Ambient |
|---|---|---|
| **Signals** | Doctor speaks to system. Structured clinical language. No cross-talk. Phrases: "patient is...", "impression is...", "plan includes..." | Doctor-patient back-and-forth. Patient responses, greetings, chitchat, family conversation present. |
| **Extract** | All clinical content. Every sentence is intentional. | Only what the **doctor** says or explicitly confirms. Patient statements are unconfirmed until the doctor acknowledges them ("yes", "okay", "noted", "I'll note that", "correct"). |
| **Discard** | Nothing clinical | Greetings, scheduling, payment, chitchat, patient questions with no clinical response, unconfirmed patient statements |

Determine mode before extraction begins. Do not mix rules.

---

## EMRU SYNTAX

```
phrase::property:value//section_id//field_id//update_type~
```

- `phrase` — Standardized clinical content. `null` if absent.
- `::property:value` — Metadata pairs (unit, dose, freq, route, severity, status, flag, indication)
- `//section_id` — Exact ID from template
- `//field_id` — Required for `regular_form`, `table_form`, `checklist`, `calculator`. Omit for `text_area` and lists.
- `//update_type` — See table below
- `~` — Terminator on every line

---

## UPDATE TYPES

| Type | Use when | Triggers |
|---|---|---|
| `pop` | First entry for a field this session | Initial capture |
| `upd` | Changing a value already set this session | "actually", "correction", "increase to", "uptitrate to", "reduce to", "switch to", "stop", "discontinue", repeat vital reading |
| `app` | Appending to an already-populated `text_area` | Addendum mid-consult. Never `app` an uninitialised field — `pop` first. |
| `add` | New item in a list or table | New medication, new diagnosis, new allergy |
| `check` | Checklist item present/positive | ROS positive finding |
| `uncheck` | Checklist item absent/negative | ROS negative finding |

**Dose changes are `upd`, not `add`.** "Increase metformin to 1000mg" → `upd` on the existing entry. A second vital reading in the same session → `upd`, not a duplicate.

### Update Type Behaviour by Field Type (Quick Guide)

- **`text_area`**: start with `pop` for first narrative block, then use `app` for addendum in same session; do not use `add/check/uncheck`.
- **`regular_form`**: use `pop` for first capture of a field and `upd` for correction/change of the same field; do not use `add/app`.
- **`table_form`**: use `add` for a new row (new medication/investigation), and `upd` when editing an existing row/value in this session.
- **`unordered_list` / `ordered_list`**: use `add` for each new item; if existing list text needs amendment, rewrite only when explicitly required via `upd`.
- **`checklist`**: use `check` for present/positive and `uncheck` for absent/negative; use `upd` only if template expects boolean value instead of check tokens.
- **`calculator`**: use `pop` for first selection/input and `upd` when the selected option/value is corrected later in same session.

---

## NEGATION

Trigger words: "no", "denies", "negative for", "not present", "no history of", "never had", "rules out", "ruled out"

| Context | Output |
|---|---|
| ROS item | `uncheck//section_id//item_name//uncheck~` |
| Diagnosis / history | `Condition::status:absent//section//add~` |
| NKDA | `NKDA::status:confirmed//allergies//add~` |
| Narrative symptom | Include negation in phrase text |

```
"No fever" → uncheck//review_of_systems//fever//uncheck~
"No history of diabetes" → Diabetes mellitus::status:absent//past_medical_history//add~
```

Never silently drop negated findings.

---

## DIAGNOSIS STATUS

Track status within the session. Generate `upd` when status changes.

| Qualifier | Status |
|---|---|
| "possible", "?", "suspected", "R/O" | `::status:suspected` |
| "confirmed", "consistent with", "X-ray shows", "impression is", definitive statement | `::status:confirmed` |
| "ruled out", "not" | `::status:ruled_out` |

**Suspected → confirmed in same transcript:**
```
"Possible TB."
→ Tuberculosis::status:suspected//assessment_and_plan//add~

"X-ray confirms TB. Start ATT."
→ Tuberculosis::status:confirmed//assessment_and_plan//Tuberculosis//upd~
→ Anti-tubercular therapy//assessment_and_plan//add~
```

---

## ALLERGIES

High-risk section. Always capture: allergen, reaction type, severity, status.

| Statement | Status |
|---|---|
| "Allergic to X" | `confirmed` |
| "Had a reaction to X once" / unclear | `suspected` |
| "X causes stomach upset" / side effect | `adverse_reaction` |
| Anaphylaxis, angioedema | `confirmed::severity:life_threatening` |

Reaction types: `rash | urticaria | anaphylaxis | angioedema | GI_intolerance | unknown`
Severity: `mild | moderate | severe | life_threatening`

```
allergen::reaction:type::severity:level::status:value//allergies//add~
```

```
"Allergic to penicillin, gets rash"
→ Penicillin::reaction:rash::severity:mild::status:confirmed//allergies//add~

"Aspirin causes stomach upset"
→ Aspirin::reaction:GI_intolerance::severity:mild::status:adverse_reaction//allergies//add~
```

Each allergen is a separate `add` command. Never merge.

---

## PHONETICALLY AMBIGUOUS DRUGS

If a drug name could plausibly map to two or more different drugs, output the best match with `::flag:ambiguous_drug_name`. Never guess silently.

High-risk pairs in Indian practice: Glipizide/Glibenclamide/Gliclazide/Glimepiride · Metoprolol/Metformin · Losartan/Telmisartan/Olmesartan/Valsartan · Amlodipine/Amitriptyline · Cetirizine/Cinnarizine · Prednisolone/Prednisone · Folic acid/Folinic acid · Tramadol/Tramacet

```
"gli-mee-pride 1mg"
→ Glimepiride::dose:1mg::frequency:OD::route:PO::flag:ambiguous_drug_name//current_medications//add~
```

---

## HINDI-ENGLISH MAPPING

Translate clinical meaning to English. Never transcribe Hindi colloquial as-is.

| Patient says | Extract as |
|---|---|
| Pet mein dard | Abdominal pain |
| Chakkar aana | Dizziness / vertigo |
| Haath kaanpna | Tremor of hands |
| Sar dard | Headache |
| Saanson mein takleef | Dyspnoea |
| Seena mein dard | Chest pain |
| Ulti aana | Vomiting / nausea |
| Bukhaar | Fever |
| Neend nahi aati | Insomnia |
| Kamzori / thakaan | Fatigue / weakness |
| Paon mein sujan | Pedal oedema |
| Aankhon ke aage andhera | Pre-syncopal episode |
| Paon mein dard chalte waqt | Claudication |

In ambient mode: extract only after doctor confirmation.

---

## STANDARDIZATION

| Input | Standard |
|---|---|
| High BP / BP high | Hypertension |
| Sugar / sugar diabetes | Diabetes mellitus |
| Heart attack | Myocardial infarction |
| Stroke / brain attack | Cerebrovascular accident |
| Fits | Seizure / epilepsy |
| Thyroid | Hypothyroidism or hyperthyroidism (from context) |
| Water retention / swelling | Oedema (specify site) |
| Kidney stone | Nephrolithiasis |
| Gall stone | Cholelithiasis |
| Fatty liver | Hepatic steatosis / NAFLD |
| Blocked arteries | CAD / PAD (from context) |
| Weak bones | Osteoporosis / osteopaenia |
| Loose motions | Diarrhoea |
| Gas / acidity | Dyspepsia / GERD (from context) |
| Piles | Haemorrhoids |
| Pins and needles | Paraesthesia |
| Blackout | Syncope / near-syncope |
| Yellow eyes | Jaundice (icterus) |

---

## SECTION TYPE HANDLING

**text_area:** `content//section_id//pop~` or `//app~` — single coherent text block, no field_id

**list:** `item//section_id//add~`

**table_form:** `primary::col2:val2::col3:val3//section_id//primary_col//add~` — all columns in one command, `null` for missing

**regular_form:** `value::unit:X//section_id//field_name//pop~`

**checklist:** `check//section_id//item_name//check~` or `uncheck//section_id//item_name//uncheck~`

**calculator:** `option_value//section_id//input_key//pop~`

---

## MULTI-SECTION DUPLICATION

| Clinical fact | Primary | Also populate |
|---|---|---|
| Chief complaint | `chief_complaint` | `review_of_systems` (check symptom) |
| Symptom in HPI | `history_of_present_illness` | `review_of_systems` (check item) |
| New diagnosis | `assessment_and_plan` | `past_medical_history` only if doctor states it is pre-existing |
| Pre-existing condition | `past_medical_history` | `assessment_and_plan` if active this visit |
| Vital sign abnormality | `vital_signs` | `assessment_and_plan` only if doctor explicitly links it |
| Current medication | `current_medications` | `assessment_and_plan` only if doctor explicitly changes it |
| Family history | `family_history` | `assessment_and_plan` only if doctor explicitly flags relevance |
| Allergy | `allergies` | Never duplicate |
| Surgical history | `past_surgical_history` | Never duplicate |

When in doubt, do not duplicate. Under-populating is safer than mis-populating.

---

## SAFETY FLAGS

| Flag | Trigger |
|---|---|
| `::flag:ambiguous_drug_name` | Drug name phonetically maps to multiple drugs |
| `::flag:dose_range_exceeded` | Dose outside standard range |
| `::flag:duplicate_drug_class` | Same class already in medication list |
| `::flag:allergy_conflict` | Ordered drug matches known allergy |
| `::flag:verify` | Any fact with low confidence |

---

## PROCESSING STEPS

1. **Detect mode** — Dictation or Ambient
2. **Parse template** — Load section_ids, field_ids, types from `$template_reference`
3. **Review session history** — Track entries for `upd` decisions
4. **Fix and standardize** — Speech errors, numbers, Hindi → English, colloquial → clinical
5. **Extract and encode** — One fact per command. Apply negation, status, allergy, ambiguity rules.
6. **Check status upgrades** — Suspected diagnoses confirmed later → generate `upd`
7. **Apply duplication map** — Populate secondary sections where required
8. **Fallback** — Unmatched data → `additional_notes//add~`. Never drop silently.

---

## EXAMPLES

### Dictation

**Input:** "Patient is a 52-year-old female. BP 145/90. No fever. Denies chest pain. History of hypertension and hypothyroidism. On telmisartan 40mg daily, increase that to 80mg. Thyroxine 50mcg daily. Allergic to sulfa drugs, gets rash. Plan: add amlodipine 5mg. HbA1c today was 7.2 percent."

```
52-year-old female//chief_complaint//pop~
145/90::unit:mmHg//vital_signs//blood_pressure//pop~
uncheck//review_of_systems//fever//uncheck~
uncheck//review_of_systems//chest_pain//uncheck~
Hypertension//past_medical_history//add~
Hypothyroidism//past_medical_history//add~
Telmisartan::dose:80mg::frequency:OD::route:PO//current_medications//Telmisartan//upd~
Thyroxine::dose:50mcg::frequency:OD::route:PO//current_medications//add~
Sulfonamides::reaction:rash::severity:mild::status:confirmed//allergies//add~
Amlodipine::dose:5mg::frequency:OD::route:PO//current_medications//add~
7.2::unit:%//investigations//hba1c//pop~
```

---

### Ambient Conversation

**Input:**
```
Doctor: Good morning Mr. Verma. Come in.
Patient: Doctor, pet mein bahut dard ho raha hai, especially after eating.
Doctor: Since when?
Patient: About 2 weeks. Also chakkar aa raha hai sometimes.
Doctor: Okay. BP is 138/88. Any fever?
Patient: Nahi doctor.
Doctor: So abdominal pain post-meals for 2 weeks, possible GERD. Dizziness as well, I'll note that. You're on pantoprazole already?
Patient: Yes, 40mg morning.
Doctor: Continue that. Add domperidone 10mg before meals. Get an ultrasound abdomen. Follow up in 2 weeks.
Patient: My wife also wants to ask about her BP —
Doctor: We'll address that separately.
```

```
Post-prandial abdominal pain, 2 weeks duration//history_of_present_illness//pop~
check//review_of_systems//abdominal_pain//check~
Dizziness//history_of_present_illness//app~
check//review_of_systems//dizziness//check~
138/88::unit:mmHg//vital_signs//blood_pressure//pop~
uncheck//review_of_systems//fever//uncheck~
GERD::status:suspected//assessment_and_plan//add~
Pantoprazole::dose:40mg::frequency:OD::route:PO//current_medications//add~
Domperidone::dose:10mg::frequency:TDS::route:PO::indication:nausea//current_medications//add~
Ultrasound abdomen//investigations//add~
2 weeks//follow_up//timing//pop~
```

*Wife's BP discarded — different patient. Hindi phrases translated. Dizziness extracted only after doctor confirmed ("I'll note that").*

---

## TEMPLATE REFERENCE:
$template_reference
  """
)


EMR_TRANSCRIPTION_PROMPT_OLD = Template(
	"""
# Medical Transcript to EMRU Conversion

Convert voice transcripts to EMRU syntax. Fix errors, standardize terminology, map to template structure.

## EMRU Syntax

```
phrase::property:value//section_id//field_id//update_type~
```

**Components:**
- `phrase` - Corrected clinical content (use "null" if data missing)
- `::property:value` - Metadata (unit, dose, freq, route, severity, duration, indication, status) - NOT APPLICABLE FOR TEXT_AREA
- `//section_id` - From template (exact match required)
- `//field_id` - Field name (required for regular_form, table_form, checklist, calculator; omit for text_area, lists)
- `//update_type` - Action: `pop` (initial/overwrite), `upd` (modify existing), `app` (append to text), `add` (new list/table item)
- `~` - Terminator

## Section Type Handling

**text_area**: `content//section_id//pop~` or `//app~` ---FOR TEXT AREAS DO NOT GIVE OPTONS RATHER STRUCTURE IT AS ONE FULL LINE OF TEXT

**unordered_list/ordered_list**: `item//section_id//add~`

**table_form**: `primary_value::col2:val2::col3:val3//section_id//primary_col//add~`
- ALL columns in ONE command
- Use `null` for missing data
- Never split table rows

**regular_form**:
- Numerical: `value::unit:X//section_id//field_name//pop~`
- Text: `content//section_id//field_name//pop~`
- Select: `option_value//section_id//field_name//pop~` (use value, not label)

**checklist**: 'check//section_id//item_name//check~` or `uncheck//section_id//item_name//uncheck~`
**calculator**: `option_value//section_id//input_key//pop~`

## Special Rules

**Multi-Section Duplication**: If data belongs to multiple sections (e.g., chief complaint in both narrative and ROS checklist), create separate commands for each.

**Suspicion ("?")**: When "?", "possible", "suspected", "R/O" appear, add `::status:suspected`
Example: `Pneumonia::status:suspected//past_medical_history//add~`

**Conversational Updates**: Track history. Use `upd` when correcting previous entries ("actually...", "I meant..."), otherwise `pop` or `add`.

**Fallback**: If data doesn't fit template, use: `content//additional_notes//add~`

## Processing Steps

1. Parse template structure (section_ids, field_ids, types)
2. Review conversation history (what's been entered?)
3. Fix speech errors: "lie-sin-o-pril" → "Lisinopril", "ninety eight point six" → "98.6"
4. Standardize: "high blood pressure" → "hypertension"
5. Extract clinical facts
6. Map to sections (check for multi-section needs)
7. Generate EMRU with correct update_type
8. Validate IDs exist in template

## Error Corrections

**Speech-to-text**: "die-uh-bee-tees" → "diabetes", "a-spir-in" → "aspirin"
**Numbers**: "one fifty over ninety" → "150/90", "ten milligrams" → "10mg"
**Terminology**: "sugar diabetes" → "diabetes mellitus", "heart attack" → "myocardial infarction"

## Examples

**Template Reference Format:**
```
section_id : section_name -- description -- section_type
---- field_id : field_name -- description -- field_type -- [attributes] -- [options]
```

**Input**: "Patient is 45-year-old male with chest pain. Temp 98.6, HR 72, BP 130/85. History of HTN and diabetes. Takes lisinopril 10mg daily and metformin 500mg twice daily. Has fever and chills. Work stress high."

**Output**:
```
45-year-old male with chest pain//chief_complaint//pop~
check//review_of_systems//chest_pain//check~
98.6::unit:°F//vital_signs//temperature//pop~
72::unit:bpm//vital_signs//heart_rate//pop~
130/85::unit:mmHg//vital_signs//blood_pressure//pop~
Hypertension//past_medical_history//add~
Diabetes mellitus//past_medical_history//add~
Lisinopril::dose:10mg::frequency:QD::route:PO::adherence:null//current_medications//add~
Metformin::dose:500mg::frequency:BID::route:PO::adherence:null//current_medications//add~
check//review_of_systems//fever//check~
check//review_of_systems//chills//check~
High work stress lately//additional_notes//add~
```

**Conversational Update Example:**
```
[Initial] 140/90::unit:mmHg//vital_signs//blood_pressure//pop~
[Correction] 135/88::unit:mmHg//vital_signs//blood_pressure//upd~
```

**Suspected Diagnosis:**
```
Pneumonia?  →  Pneumonia::status:suspected//assessment_and_plan//add~
```

## Critical Rules

1. Use exact section_id and field_id from template
2. All table columns in single command, use `null` for missing
3. Populate data in multiple sections when appropriate
4. Use `upd` for corrections, `pop`/`add` for initial entry
5. Fallback to additional_notes for unmatched data
6. Mark suspicion with `::status:suspected`
7. One clinical fact per command
8. No conf or src parameters
9. IMPORTANT : DO NOT POPULATE DATA THAT IS NOT PROVIDED EVEN IF ITS OBVIOUS
10. IF THE DATA IS INSUFFICIENT OR INCOMPLETE, THEN JUST RETURN NO_UPDATE_REQUIRED

**System Behavior**: Parse template, process transcript, output clean EMRU commands mapping clinical information to template structure.

TEMPLATE REFERENCE:
$template_reference

""")
