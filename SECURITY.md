# Security

Report a vulnerability privately to the maintainers. Do not open a public issue that includes keys, patient data or a working exploit.

- Patient-portal `dev_fixed` OTP is accepted only when `CLINESTRA_ENV=development`.
- Production mode (`CLINESTRA_MODE=production`) refuses plaintext API key files and a master key stored next to the database. Set `CLINESTRA_MASTER_KEY` and `CLINESTRA_KEY_PEPPER`.
- `clinestra keys import` hashes a plaintext key file. Delete the plaintext file afterwards and rotate any key that has been stored in the clear.
- Outbound AI defaults to `send_patient_identifiers: false`. An external model route also needs `inference_geo`.
- Do not commit `*.db`, `*.key`, `keys.json`, `LOGINS*.md` or `.env*`.
