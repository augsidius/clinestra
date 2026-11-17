# Changelog

## Unreleased

- Records made from a `cohort_key`, `protocol_key` or `plugin_key` now store `definition_id` and `definition_version`. Previously only records created with `definition_key` did.
- Cohort entry and exit actions use the definition version the patient enrolled under, even if it has been retired. Records with no stored version still use the latest.
- In production mode, those same records now require a clinically approved definition, as `definition_key` records already did.

## 0.3.0

- Removed the `care_model` concept, its provider interface and its routes. Concepts are now 31.
- The MDD pack is no longer installed. It lives in `examples/packs/mdd`; load it by path. The default packs are `core` only.
- `CLINESTRA_ADMIN_ALL_ROLES` is off by default, so an admin no longer acts as a clinician unless it is set to `1`.
- Install extras: `clinestra[full]` is the API and AI install. `[all]` is an alias of it and no longer installs test tools. Contributors use `[dev]`.
- `clinestra demo` runs on the core pack.
- The source package and wheel include `LICENSE`.
- New commands: `doctor`, `audit verify`, `schema`, `pack new`, `pack validate`. `concepts` and `packs` gain `--json`; `packs` prints a table by default. `serve --dev` is refused on non-local hosts. Errors are one line, with exit code 1.

## 0.2.0

- Regimens start as `proposed` and create medications only after a second clinician counter-signs and the regimen becomes `active`. Orders from a regimen are proposed care actions and are not dispatched.
- Definitions carry review metadata. Protocols, regimens, reactions and cohorts cannot be published without a citation and an approval from someone other than the author.
- Production mode refuses unapproved definitions, plaintext API keys, and a master key file next to the database.
- Portal OTP answers do not reveal whether a phone is registered. `dev_fixed` is limited to development. Sessions from a previous process epoch are rejected.
- API keys can be stored as HMAC-SHA256 hashes (`clinestra keys import`). New keys must be at least 32 characters.
- Calculator and assessment runs return a `usage_id`. A null reference range does not raise a flag. Expression errors return 422.
- The default AI policy does not send patient identifiers.
