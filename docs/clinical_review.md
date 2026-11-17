# Reviewing clinical content

Templates decide what care teams are prompted to do: protocols, questionnaires, calculators, rules and case sheets. Clinicians at the deploying organisation should review them before use with patients.

## Pilot and production

In **pilot** mode, unreviewed content can run. Use it only for testing. In **production** mode, unreviewed content is not allowed.

## The review

1. Your technical team exports a review sheet for a pack:

   ```bash
   clinestra pack review-sheet my_pack --out review.csv
   ```

2. Clinicians record a decision for each item, with notes and a reference such as a committee minute.
3. The technical team records those decisions.

A template's creator cannot review it. Only a clinical lead or administrator can record a review.

## What to check

- Is it right for your patients and setting?
- Are the sources current, and do they apply locally?
- Are the thresholds and bands correct?
- What happens when a result is urgent, and who sees it?
- Does a clinician make every clinical decision it implies?

## Example content

Example packs are for learning how Clinestra works. They are not clinically validated, and must not be used for patient care without your own review.

## After publishing

A published template is frozen. To change one, publish a new version. Review content again when guidelines or local practice change.
